"""Durable Drive-to-YouTube queue using Firestore and Cloud Tasks.

Required env: GCP_PROJECT, TASKS_LOCATION, TASKS_QUEUE, TASKS_SERVICE_ACCOUNT,
TASKS_TARGET_URL, TOKEN_ENCRYPTION_KEY (Fernet key). Cloud Run must grant
roles/run.invoker to TASKS_SERVICE_ACCOUNT.
"""
import datetime
import json
import os
import secrets
import time
from urllib.parse import urlparse

import google.auth.transport.requests
import google.oauth2.id_token
import requests
from cryptography.fernet import Fernet
from flask import Blueprint, abort, jsonify, request, session
from google.cloud import firestore, tasks_v2
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

queue_bp = Blueprint("queue", __name__)
_db = None
_tasks = None

def db():
    global _db
    if _db is None:
        _db = firestore.Client(project=os.environ["GCP_PROJECT"])
    return _db

def task_client():
    global _tasks
    if _tasks is None:
        _tasks = tasks_v2.CloudTasksClient()
    return _tasks

def cipher():
    return Fernet(os.environ["TOKEN_ENCRYPTION_KEY"].encode())

def identity():
    sid = session.get("sid")
    if not sid:
        abort(401)
    return sid

def authorized_job(job):
    if job.get("owner") != identity():
        abort(404)

def csrf_check():
    if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), session.get("csrf", "missing")):
        abort(403)

def enqueue(job_id):
    project = os.environ["GCP_PROJECT"]
    location = os.environ["TASKS_LOCATION"]
    queue = os.environ["TASKS_QUEUE"]
    target = os.environ["TASKS_TARGET_URL"].rstrip("/") + "/tasks/upload"
    account = os.environ["TASKS_SERVICE_ACCOUNT"]
    parent = task_client().queue_path(project, location, queue)
    task = {"http_request": {
        "http_method": tasks_v2.HttpMethod.POST,
        "url": target,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"job_id": job_id}).encode(),
        "oidc_token": {"service_account_email": account, "audience": os.environ["TASKS_TARGET_URL"].rstrip("/")},
    }}
    task["dispatch_deadline"] = {"seconds": 1800}
    return task_client().create_task(parent=parent, task=task)

@queue_bp.post("/api/queue")
def add_jobs():
    csrf_check()
    owner = identity()
    payload = request.get_json(silent=True) or {}
    items = payload.get("items", [])
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        abort(400, "Select 1–100 videos")
    from app import require_creds
    credentials = require_creds()
    if not credentials.refresh_token:
        abort(400, "Sign out and sign in again to grant offline access")
    from googleapiclient.discovery import build
    drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
    created = []
    for item in items:
        fid = item.get("id", "") if isinstance(item, dict) else ""
        if not isinstance(fid, str) or not fid or len(fid) > 200:
            abort(400, "Invalid file")
        meta = drive.files().get(fileId=fid, fields="id,name,mimeType,size").execute()
        if not meta.get("mimeType", "").startswith("video/") or int(meta.get("size", 0)) <= 0:
            abort(400, "Not an uploadable video")
        title = (item.get("title") or meta["name"].rsplit(".", 1)[0]).strip()[:100]
        if not title:
            abort(400, "Title required")
        # Avoid duplicate active jobs for the same owner/file.
        existing = list(db().collection("upload_jobs").where("owner", "==", owner).where("file_id", "==", fid).limit(100).stream())
        active = next((x for x in existing if x.to_dict().get("status") in ("queued", "uploading", "completed")), None)
        if active:
            created.append({"id": active.id, "status": "already_queued"})
            continue
        jid = secrets.token_urlsafe(20)
        doc = db().collection("upload_jobs").document(jid)
        record = {
            "owner": owner, "file_id": fid, "name": meta["name"], "title": title,
            "size": int(meta["size"]), "mime_type": meta["mimeType"],
            "status": "queued", "offset": 0, "location": None,
            "token": cipher().encrypt(credentials.to_json().encode()).decode(),
            "created_at": firestore.SERVER_TIMESTAMP, "updated_at": firestore.SERVER_TIMESTAMP,
        }
        doc.set(record)
        try:
            enqueue(jid)
        except Exception:
            doc.update({"status": "enqueue_failed"})
            raise
        created.append({"id": jid, "status": "queued"})
    return jsonify({"jobs": created}), 202

@queue_bp.get("/api/queue")
def list_jobs():
    owner = identity()
    docs = db().collection("upload_jobs").where("owner", "==", owner).limit(100).stream()
    items = []
    for doc in docs:
        v = doc.to_dict()
        items.append({"id": doc.id, "name": v.get("name"), "status": v.get("status"),
                      "offset": v.get("offset", 0), "size": v.get("size", 0),
                      "video_id": v.get("video_id"), "error": v.get("error")})
    return jsonify({"jobs": items})

def verify_task():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        abort(401)
    try:
        info = google.oauth2.id_token.verify_oauth2_token(
            auth[7:], google.auth.transport.requests.Request(),
            audience=os.environ["TASKS_TARGET_URL"].rstrip("/"))
    except Exception:
        abort(401)
    if info.get("email") != os.environ["TASKS_SERVICE_ACCOUNT"]:
        abort(403)

@queue_bp.post("/tasks/upload")
def run_upload():
    verify_task()
    payload = request.get_json(silent=True) or {}
    jid = payload.get("job_id")
    if not isinstance(jid, str):
        abort(400)
    ref = db().collection("upload_jobs").document(jid)
    snap = ref.get()
    if not snap.exists:
        return "", 204
    job = snap.to_dict()
    if job["status"] == "completed":
        return "", 204
    if job["status"] not in ("queued", "uploading"):
        return "", 204
    c = Credentials.from_authorized_user_info(json.loads(cipher().decrypt(job["token"].encode())), scopes=[
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/youtube.upload", "openid",
        "https://www.googleapis.com/auth/userinfo.email"])
    def token():
        if not c.valid or c.expired:
            c.refresh(Request())
            ref.update({"token": cipher().encrypt(c.to_json().encode()).decode()})
        return c.token
    size = int(job["size"])
    offset = int(job.get("offset") or 0)
    location = job.get("location")
    if not location:
        metadata = {"snippet": {"title": job["title"], "categoryId": "10"},
                    "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False}}
        r = requests.post("https://www.googleapis.com/upload/youtube/v3/videos",
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={"Authorization": "Bearer " + token(), "X-Upload-Content-Type": job["mime_type"],
                     "X-Upload-Content-Length": str(size)},
            json=metadata, timeout=60)
        r.raise_for_status()
        location = r.headers["Location"]
        host = urlparse(location).hostname or ""
        if host not in ("www.googleapis.com", "youtube.googleapis.com"):
            abort(502, "Invalid resumable URL")
        ref.update({"location": location, "status": "uploading", "updated_at": firestore.SERVER_TIMESTAMP})
    else:
        # Query server's committed offset after a retry / interrupted worker.
        r = requests.put(location, headers={"Content-Length": "0",
                         "Content-Range": "bytes */" + str(size),
                         "Authorization": "Bearer " + token()}, timeout=60)
        if r.status_code in (200, 201):
            ref.update({"status": "completed", "video_id": r.json().get("id"),
                        "updated_at": firestore.SERVER_TIMESTAMP})
            return "", 204
        if r.status_code == 308:
            rng = r.headers.get("Range", "")
            offset = int(rng.rsplit("-", 1)[-1]) + 1 if "-" in rng else 0
        else:
            r.raise_for_status()
            abort(502)
    # Keep each dispatch below the HTTP deadline, then enqueue continuation.
    deadline = time.monotonic() + 20 * 60
    while offset < size:
        if time.monotonic() >= deadline:
            enqueue(jid)
            return '', 204
        end = min(size, offset + 8 * 1024 * 1024) - 1
        chunk = requests.get("https://www.googleapis.com/drive/v3/files/" + job["file_id"],
            params={"alt": "media"}, headers={"Authorization": "Bearer " + token(),
            "Range": f"bytes={offset}-{end}"}, timeout=180)
        chunk.raise_for_status()
        if chunk.status_code != 206 and (offset != 0 or len(chunk.content) != size):
            abort(502, "Drive did not honor requested range")
        if len(chunk.content) != end - offset + 1:
            abort(502, "Drive range mismatch")
        resp = requests.put(location, headers={"Authorization": "Bearer " + token(),
            "Content-Length": str(len(chunk.content)),
            "Content-Range": f"bytes {offset}-{end}/{size}", "Content-Type": job["mime_type"]},
            data=chunk.content, timeout=180)
        if resp.status_code not in (200, 201, 308):
            resp.raise_for_status()
            abort(502, "Unexpected YouTube response")
        if resp.status_code == 308:
            rng = resp.headers.get('Range', '')
            offset = int(rng.rsplit('-', 1)[-1]) + 1 if '-' in rng else 0
        else:
            offset = end + 1
        update = {"offset": offset, "updated_at": firestore.SERVER_TIMESTAMP}
        if resp.status_code in (200, 201):
            update.update({"status": "completed", "video_id": resp.json().get("id")})
        ref.update(update)
    return "", 204
