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
CSS="<style>body{font:16px system-ui;background:#10151e;color:#f8fafc;max-width:740px;margin:24px auto;padding:12px}a{color:#a9c9ff}input,textarea,button{font:inherit;padding:12px;box-sizing:border-box;width:100%;margin:8px 0 16px;border-radius:8px}input,textarea{background:#242d3b;color:white;border:1px solid #758398}button{background:#8fb4ff;border:0;font-weight:600}.item{padding:15px 0;border-bottom:1px solid #4b5563}small{color:#aab3c0}</style>"
@app.route("/")
def index():
    c=creds()
    if c is None:return CSS+'<h1>Drive → YouTube</h1><p>Upload your Google Drive performances to YouTube.</p><a href="/login">Sign in with Google</a>'
    drive=build("drive","v3",credentials=c,cache_discovery=False)
    data=drive.files().list(q="mimeType contains 'video/' and trashed = false",fields="nextPageToken,files(id,name,mimeType,size)",pageSize=100).execute()
    template=CSS+'<h1>Drive → YouTube</h1><p>Choose a video to upload privately.</p>{% for f in files %}<div class="item"><a href="{{url_for(\'edit\',file_id=f.id)}}">{{f.name}}</a></div>{% endfor %}<p><small>Showing up to 100 videos.</small></p><a href="/logout">Sign out</a>'
    return render_template_string(template,files=data.get("files",[]))
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
