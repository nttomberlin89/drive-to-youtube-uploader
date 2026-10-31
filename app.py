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
SCOPES = ["https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/youtube.upload", "openid", "https://www.googleapis.com/auth/userinfo.email"]
# MVP only: ephemeral tokens, one instance, lost on restart. Replace with durable encrypted token storage for production.
TOKENS = {}
def oauth_flow():
    cfg={"web":{"client_id":os.environ["GOOGLE_CLIENT_ID"],"client_secret":os.environ["GOOGLE_CLIENT_SECRET"],"auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token","redirect_uris":[url_for("callback",_external=True)]}}
    return Flow.from_client_config(cfg,scopes=SCOPES,redirect_uri=url_for("callback",_external=True))
def creds():
    rec=TOKENS.get(session.get("sid"))
    if not rec:return None
    c=Credentials.from_authorized_user_info(rec,SCOPES)
    if c.expired and c.refresh_token:
        c.refresh(Request());TOKENS[session["sid"]]=json.loads(c.to_json())
    return c
def require_creds():
    c=creds()
    if c is None:abort(401,"Please sign in again.")
    return c
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
    files=[]
    page=None
    # Follow Drive pagination rather than silently omitting everything after 100.
    while True:
        data=drive.files().list(q="mimeType contains 'video/' and trashed = false",
            fields="nextPageToken,files(id,name,mimeType,size,thumbnailLink,webViewLink)",
            pageSize=1000,pageToken=page).execute()
        files.extend(data.get("files",[]))
        page=data.get("nextPageToken")
        if not page or len(files)>=3000:break
    session["csrf"]=secrets.token_urlsafe(24)
    template=CSS+"""
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
    <p>Selected videos upload sequentially with their filenames as titles. Keep this page open until all finish.</p>
    <button id="upload" type="button" onclick="uploadSelected()" disabled>Upload selected privately</button>
    <div id="status" role="status" aria-live="polite"></div></div>
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
      if(!confirm('Upload '+chosen.length+' video(s) to YouTube as PRIVATE?'))return;
      upload.disabled=true;search.disabled=true;
      let completed=0,failed=0;
      for(let i=0;i<chosen.length;i++){
        const t=chosen[i], name=t.dataset.name;
        status.textContent+='\n['+(i+1)+'/'+chosen.length+'] Uploading '+name+' ...';
        try{
          const form=new FormData();form.set('csrf',csrf);form.set('title',name.replace(/\.[^.]+$/,'').slice(0,100));form.set('description','');form.set('tags','');
          const response=await fetch('/upload/'+encodeURIComponent(t.dataset.id),{method:'POST',body:form,credentials:'same-origin'});
          if(!response.ok)throw new Error('HTTP '+response.status+' — '+(await response.text()).replace(/<[^>]*>/g,' ').slice(0,160));
          completed++;status.textContent+=' done';
          t.querySelector('.pick').checked=false;t.classList.remove('selected');
        }catch(e){failed++;status.textContent+=' FAILED: '+e.message}
      }
      status.textContent+='\nFinished: '+completed+' succeeded, '+failed+' failed.';
      search.disabled=false;update();
    }
    </script>"""
    return render_template_string(template,files=files[:3000],truncated=len(files)>=3000,csrf=session["csrf"])

@app.route("/thumbnail/<file_id>")
def thumbnail(file_id):
    c=require_creds()
    drive=build("drive","v3",credentials=c,cache_discovery=False)
    f=drive.files().get(fileId=file_id,fields="id,mimeType,thumbnailLink").execute()
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
    sid=secrets.token_urlsafe(32);session["sid"]=sid;TOKENS[sid]=json.loads(c.to_json())
    return redirect("/")
@app.route("/edit/<file_id>")
def edit(file_id):
    drive=build("drive","v3",credentials=require_creds(),cache_discovery=False)
    f=drive.files().get(fileId=file_id,fields="id,name,mimeType,size").execute()
    if not f["mimeType"].startswith("video/"):abort(400)
    session['csrf']=secrets.token_urlsafe(24)
    template=CSS+'<a href="/">Back</a><h2>{{name}}</h2><form action="{{url_for(\'upload\',file_id=id)}}" method="post"><input type="hidden" name="csrf" value="{{csrf}}"><label>Title<input required maxlength="100" name="title" value="{{name}}"></label><label>Description and hashtags<textarea name="description" rows="6"></textarea></label><label>Tags (comma separated)<input name="tags"></label><p>Privacy: <b>Private</b></p><button type="submit">Upload to YouTube</button></form><small>Keep this page open until upload finishes. Uploads may take several minutes.</small>'
    return render_template_string(template,id=file_id,name=f["name"],csrf=session['csrf'])
@app.route("/upload/<file_id>",methods=["POST"])
def upload(file_id):
    if not secrets.compare_digest(request.form.get('csrf',''),session.get('csrf','missing')):abort(403,'Invalid form token')
    c=require_creds()
    d=build("drive","v3",credentials=c,cache_discovery=False)
    f=d.files().get(fileId=file_id,fields="id,name,mimeType,size").execute()
    if not f["mimeType"].startswith("video/"):abort(400)
    size=int(f.get("size",0))
    if size<=0:abort(400,"Video size is unavailable")
    title=request.form.get("title","").strip()[:100]
    if not title:abort(400)
    metadata={"snippet":{"title":title,"description":request.form.get("description","")[:5000],"tags":[x.strip() for x in request.form.get("tags","").split(",") if x.strip()],"categoryId":"10"},"status":{"privacyStatus":"private","selfDeclaredMadeForKids":False}}
    headers={"Authorization":"Bearer "+c.token,"X-Upload-Content-Type":f["mimeType"],"X-Upload-Content-Length":str(size)}
    init=requests.post("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",headers=headers,json=metadata,timeout=60)
    init.raise_for_status()
    location=init.headers["Location"]
    if not location.startswith('https://www.googleapis.com/') and not location.startswith('https://youtube.googleapis.com/'):
        abort(502,'Unexpected YouTube upload endpoint')
    offset=0
    while offset<size:
        end=min(size,offset+8*1024*1024)-1
        if c.expired:
            c.refresh(Request());TOKENS[session["sid"]]=json.loads(c.to_json())
        chunk=requests.get("https://www.googleapis.com/drive/v3/files/"+file_id,params={"alt":"media"},headers={"Authorization":"Bearer "+c.token,"Range":f"bytes={offset}-{end}"},timeout=180)
        chunk.raise_for_status()
        if len(chunk.content)!=end-offset+1:abort(502,"Drive returned an unexpected chunk size")
        resp=requests.put(location,headers={"Content-Length":str(len(chunk.content)),"Content-Range":f"bytes {offset}-{end}/{size}","Content-Type":f["mimeType"]},data=chunk.content,timeout=180)
        if resp.status_code not in (200,201,308):abort(502,"YouTube upload error: "+resp.text[:300])
        offset=end+1
    return CSS+'<h2>Upload completed</h2><p>Video was submitted as private.</p><a href="/">Back to Drive videos</a>'
@app.route("/logout")
def logout():
    TOKENS.pop(session.get("sid"),None);session.clear();return redirect("/")
@app.route("/health")
def health():return "ok"
