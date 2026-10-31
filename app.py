import os, secrets, json, time
import requests
from flask import Flask, request, redirect, session, url_for, render_template_string, abort
from werkzeug.middleware.proxy_fix import ProxyFix
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.environ["FLASK_SECRET_KEY"]
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
from background_queue import queue_bp
app.register_blueprint(queue_bp)
SCOPES = ["https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly", "openid", "https://www.googleapis.com/auth/userinfo.email"]
from background_queue import db, cipher
from google.cloud import firestore
def oauth_flow():
    cfg={"web":{"client_id":os.environ["GOOGLE_CLIENT_ID"],"client_secret":os.environ["GOOGLE_CLIENT_SECRET"],"auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token","redirect_uris":[url_for("callback",_external=True)]}}
    return Flow.from_client_config(cfg,scopes=SCOPES,redirect_uri=url_for("callback",_external=True))
def creds():
    sid=session.get("sid")
    if not sid:return None
    snap=db().collection("oauth_sessions").document(sid).get()
    if not snap.exists:return None
    info=json.loads(cipher().decrypt(snap.to_dict()["token"].encode()))
    c=Credentials.from_authorized_user_info(info, info.get("scopes"))
    if c.expired and c.refresh_token:
        c.refresh(Request());db().collection("oauth_sessions").document(sid).update({"token":cipher().encrypt(c.to_json().encode()).decode()})
    return c
def account_identity():
    """Use Google's stable account ID, including for existing browser sessions."""
    c=require_creds()
    ref=db().collection("oauth_sessions").document(session["sid"])
    saved=ref.get().to_dict()
    if saved.get("account_id"):
        return saved["account_id"]
    r=requests.get("https://openidconnect.googleapis.com/v1/userinfo",headers={"Authorization":"Bearer "+c.token},timeout=20)
    r.raise_for_status()
    profile=r.json()
    if not profile.get("email_verified") or profile.get("email","").lower()!=os.environ.get("ALLOWED_EMAIL","").strip().lower():
        abort(403)
    ref.update({"account_id":profile["sub"]})
    return profile["sub"]
def require_creds():
    c=creds()
    if c is None:abort(401,"Please sign in again.")
    return c
# Only videos within the approved Drums folder (including nested folders).
ALLOWED_DRIVE_FOLDER_IDS = ("1-HbgVClsa0SwLjhleegjRSwV0zrt0g-I",)

def allowed_video(drive, file_id):
    """Return metadata only when a video descends from an approved folder."""
    from googleapiclient.errors import HttpError
    try:
        meta = drive.files().get(fileId=file_id, fields="id,name,mimeType,size,parents,thumbnailLink,webViewLink").execute()
        if not meta.get("mimeType", "").startswith("video/"):
            abort(404)
        pending = list(meta.get("parents", []))
        visited = set()
        while pending:
            folder_id = pending.pop()
            if folder_id in ALLOWED_DRIVE_FOLDER_IDS:
                return meta
            if folder_id in visited:
                continue
            visited.add(folder_id)
            parent = drive.files().get(fileId=folder_id, fields="id,parents,mimeType").execute()
            pending.extend(parent.get("parents", []))
    except HttpError as exc:
        if exc.resp.status in (403, 404):
            abort(404)
        raise
    abort(404, "Video is outside approved Drive folders")

def approved_videos(drive, limit=3000):
    """List videos by traversing approved folders, never searching the whole Drive."""
    folders = list(ALLOWED_DRIVE_FOLDER_IDS)
    seen_folders = set()
    seen_videos = set()
    videos = []
    while folders and len(videos) < limit:
        folder_id = folders.pop(0)
        if folder_id in seen_folders:
            continue
        seen_folders.add(folder_id)
        page = None
        while True:
            q = "'" + folder_id.replace("'", "\\'") + "' in parents and trashed = false"
            result = drive.files().list(q=q, fields="nextPageToken,files(id,name,mimeType,size,thumbnailLink,webViewLink)",
                                        pageSize=1000, pageToken=page).execute()
            for item in result.get("files", []):
                if item.get("mimeType") == "application/vnd.google-apps.folder":
                    folders.append(item["id"])
                elif item.get("mimeType", "").startswith("video/") and item["id"] not in seen_videos:
                    seen_videos.add(item["id"])
                    videos.append(item)
                    if len(videos) >= limit:
                        break
            page = result.get("nextPageToken")
            if not page or len(videos) >= limit:
                break
    return videos

CSS="""<style>
*{box-sizing:border-box}body{font:16px system-ui;background:#10151e;color:#f8fafc;max-width:1050px;margin:24px auto;padding:16px}
a{color:#a9c9ff}input,textarea,button{font:inherit;padding:12px;border-radius:8px}input,textarea{background:#242d3b;color:white;border:1px solid #758398}
button{background:#8fb4ff;border:0;font-weight:600;cursor:pointer}button:disabled{opacity:.5;cursor:default}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:20px 0}.toolbar button{width:auto}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:16px}
.tile{background:#1c2635;border:2px solid transparent;border-radius:12px;overflow:hidden;cursor:pointer}.tile.selected{border-color:#8fb4ff}
.tile img{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;background:#303b4a}
.tile .details{padding:10px;overflow-wrap:anywhere}.tile small{color:#aab3c0}
.tile input{width:auto;margin:0 8px 0 0;accent-color:#8fb4ff}.tile .name{font-weight:600}
.queue{background:#1c2635;padding:16px;border-radius:12px;margin-top:24px}.queue div{padding:5px 0}
small{color:#aab3c0}label{display:block}#status{white-space:pre-wrap}
</style>"""
@app.route("/")
def index():
    c=creds()
    if c is None:
        return CSS+'<h1>Drive → YouTube</h1><p>Upload your Google Drive performances to YouTube.</p><a href="/login">Sign in with Google</a>'
    drive=build("drive","v3",credentials=c,cache_discovery=False)
    files=approved_videos(drive)
    session["csrf"]=secrets.token_urlsafe(24)
    template=CSS+r"""
    <h1>Drive → YouTube</h1>
    <p>Select multiple videos. Uploads are private by default.</p>
    <div class="toolbar">
      <input id="search" type="search" placeholder="Search videos" aria-label="Search videos">
      <button type="button" onclick="selectVisible(true)">Select visible</button>
      <button type="button" onclick="selectVisible(false)">Clear visible</button>
      <strong id="count">0 selected</strong>
    </div>
    <div class="gallery">
    {% for f in files %}
      <div class="tile" data-id="{{f.id}}" data-name="{{f.name|e}}">
        <img src="{{url_for('thumbnail',file_id=f.id)}}" alt="" loading="lazy">
        <div class="details"><label><input type="checkbox" class="pick" aria-label="Select {{f.name|e}}"><span class="name">{{f.name}}</span></label>
        <small>{% if f.size %}{{(f.size|int / 1048576)|round(1)}} MB{% endif %}</small>
        <div><a href="{{url_for('edit',file_id=f.id)}}">Edit / upload individually</a>
        {% if f.webViewLink %} · <a href="{{f.webViewLink}}" target="_blank" rel="noopener">Preview in Drive</a>{% endif %}</div></div>
      </div>
    {% endfor %}
    </div>
    <div class="queue"><h2>Batch upload</h2>
    <p>Uploads run on Google Cloud. You can close this page after queueing; return later to check progress.</p>
    <button id="upload" type="button" onclick="uploadSelected()" disabled>Upload selected privately</button>
    <div id="status" role="status" aria-live="polite"></div><h3>Saved upload queue</h3><pre id="joblist" style="white-space:pre-wrap"></pre><button type="button" onclick="refreshQueue()">Refresh status</button></div>
    <p><small>Showing {{files|length}} videos{% if truncated %} (limited to first 3,000){% endif %}.</small></p>
    <a href="/logout">Sign out</a>
    <script>
    const csrf={{csrf|tojson}};
    const tiles=[...document.querySelectorAll('.tile')];
    const search=document.getElementById('search');
    const upload=document.getElementById('upload');
    const status=document.getElementById('status');
    function visible(t){return t.style.display!=='none'}
    function update(){let n=document.querySelectorAll('.pick:checked').length;document.getElementById('count').textContent=n+' selected';upload.disabled=n===0}
    search.addEventListener('input',()=>{const q=search.value.toLowerCase();tiles.forEach(t=>t.style.display=t.dataset.name.toLowerCase().includes(q)?'':'none')});
    document.querySelectorAll('.pick').forEach(c=>c.addEventListener('change',()=>{c.closest('.tile').classList.toggle('selected',c.checked);update()}));
    function selectVisible(v){tiles.filter(visible).forEach(t=>{let c=t.querySelector('.pick');c.checked=v;t.classList.toggle('selected',v)});update()}
    async function uploadSelected(){
      const chosen=tiles.filter(t=>t.querySelector('.pick').checked);
      if(!confirm('Queue '+chosen.length+' video(s) to YouTube as PRIVATE?'))return;
      upload.disabled=true;
      try {
        const items=chosen.map(t=>({id:t.dataset.id,title:t.dataset.name.replace(/\.[^.]+$/,'').slice(0,100)}));
        const response=await fetch('/api/queue',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({items})});
        if(!response.ok)throw Error('Could not queue: HTTP '+response.status+' '+(await response.text()).slice(0,160));
        status.textContent='Queued successfully. You may close this window. Check back for progress.';
        chosen.forEach(t=>{t.querySelector('.pick').checked=false;t.classList.remove('selected')});
      }catch(e){status.textContent=e.message}
      update();refreshQueue();
    }
    async function refreshQueue(){
      try{
        const response=await fetch('/api/queue');
        if(!response.ok)return;
        const data=await response.json();
        const lines=(data.jobs||[]).map(j=>j.name+' — '+j.status.replaceAll('_',' ')+' ('+Math.round(100*(j.offset||0)/(j.size||1))+'%)'+(j.error?' '+j.error:'')+(j.next_retry_at?' Next check: '+new Date(j.next_retry_at).toLocaleString():'')+(j.video_id?' https://youtu.be/'+j.video_id:''));
        document.getElementById('joblist').textContent=lines.join('\n');
      }catch(e){}
    }
    refreshQueue();
    setInterval(refreshQueue,15000);
    </script>"""
    return render_template_string(template,files=files[:3000],truncated=len(files)>=3000,csrf=session["csrf"])

@app.route("/thumbnail/<file_id>")
def thumbnail(file_id):
    c=require_creds()
    drive=build("drive","v3",credentials=c,cache_discovery=False)
    f=allowed_video(drive, file_id)
    if not f.get("mimeType","").startswith("video/"):abort(404)
    url=f.get("thumbnailLink")
    if not url:abort(404)
    from urllib.parse import urlparse
    # Never fetch arbitrary URLs returned by an untrusted source.
    host=(urlparse(url).hostname or "").lower()
    if host not in ("lh3.googleusercontent.com","drive.google.com","docs.google.com") and not host.endswith(".googleusercontent.com"):
        abort(502,"Unexpected thumbnail host")
    r=requests.get(url,headers={"Authorization":"Bearer "+c.token},timeout=15)
    r.raise_for_status()
    if not r.headers.get("Content-Type","").startswith("image/") or len(r.content)>4*1024*1024:abort(502)
    from flask import Response
    return Response(r.content,content_type=r.headers["Content-Type"],headers={"Cache-Control":"private, max-age=300"})

@app.route("/login")
def login():
    f=oauth_flow()
    link,state=f.authorization_url(access_type="offline",prompt="consent")
    session["state"]=state
    return redirect(link)
@app.route("/callback")
def callback():
    if not request.args.get("state") or request.args.get("state")!=session.pop("state",None):abort(400,"Invalid OAuth state")
    f=oauth_flow()
    f.fetch_token(authorization_response=request.url)
    c=f.credentials
    r=requests.get("https://openidconnect.googleapis.com/v1/userinfo",headers={"Authorization":"Bearer "+c.token},timeout=20)
    r.raise_for_status()
    profile=r.json()
    allowed=os.environ.get("ALLOWED_EMAIL","").strip().lower()
    if not allowed or not profile.get("email_verified") or profile.get("email","").lower()!=allowed:abort(403,"This Google account is not authorized")
    sid=secrets.token_urlsafe(32);session["sid"]=sid
    db().collection("oauth_sessions").document(sid).set({"token":cipher().encrypt(c.to_json().encode()).decode(),"account_id":profile["sub"],"created_at":firestore.SERVER_TIMESTAMP})
    from background_queue import reconnect_jobs
    reconnect_jobs(profile["sub"], c)
    return redirect("/")
@app.route("/edit/<file_id>")
def edit(file_id):
    drive=build("drive","v3",credentials=require_creds(),cache_discovery=False)
    f=allowed_video(drive, file_id)
    if not f["mimeType"].startswith("video/"):abort(400)
    session['csrf']=secrets.token_urlsafe(24)
    template=CSS+'<a href="/">Back</a><h2>{{name}}</h2><form action="{{url_for(\'upload\',file_id=id)}}" method="post"><input type="hidden" name="csrf" value="{{csrf}}"><label>Title<input required maxlength="100" name="title" value="{{name}}"></label><label>Description and hashtags<textarea name="description" rows="6"></textarea></label><label>Tags (comma separated)<input name="tags"></label><p>Privacy: <b>Private</b></p><button type="submit">Upload to YouTube</button></form><small>Uploads continue in the background. Check the saved queue for progress.</small>'
    return render_template_string(template,id=file_id,name=f["name"],csrf=session['csrf'])
@app.route("/upload/<file_id>",methods=["POST"])
def upload(file_id):
    if not secrets.compare_digest(request.form.get('csrf',''),session.get('csrf','missing')):abort(403,'Invalid form token')
    c=require_creds()
    from background_queue import queue_video
    queue_video(c, {"id":file_id,"title":request.form.get("title", ""),
                   "description":request.form.get("description", ""),
                   "tags":[x.strip() for x in request.form.get("tags", "").split(",") if x.strip()]})
    return redirect("/", code=303)
@app.route("/logout")
def logout():
    if session.get("sid"):db().collection("oauth_sessions").document(session["sid"]).delete()
    session.clear();return redirect("/")
@app.route("/health")
def health():return "ok"

