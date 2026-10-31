"""Durable Drive-to-YouTube queue using Firestore and Cloud Tasks.

Required env: GCP_PROJECT, TASKS_LOCATION, TASKS_QUEUE, TASKS_SERVICE_ACCOUNT,
TASKS_TARGET_URL, TOKEN_ENCRYPTION_KEY (Fernet key). Cloud Run must grant
roles/run.invoker to TASKS_SERVICE_ACCOUNT.
"""
import datetime
import hashlib
import json
import os
import re
import secrets
import time
from urllib.parse import urlparse

import google.auth.transport.requests
import google.oauth2.id_token
import requests
from cryptography.fernet import Fernet
from flask import Blueprint, abort, jsonify, request, session, current_app
from google.cloud import firestore, tasks_v2
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from werkzeug.exceptions import HTTPException

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
    from app import account_identity
    return account_identity()

def authorized_job(job):
    if job.get("owner") != identity():
        abort(404)

def csrf_check():
    if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), session.get("csrf", "missing")):
        abort(403)

def enqueue(job_id, schedule_at=None):
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
    task["dispatch_deadline"] = {"seconds": 900}
    if schedule_at is not None:
        task["schedule_time"] = schedule_at
    return task_client().create_task(parent=parent, task=task)

def file_lock(file_id):
    return db().collection("upload_locks").document(hashlib.sha256(file_id.encode()).hexdigest())

def now():
    return datetime.datetime.now(datetime.timezone.utc)

def queue_video(credentials, item):
    """Reserve one durable job per Drive file, across sign-ins and both UIs."""
    owner = identity()
    if not credentials.refresh_token:
        abort(400, "Sign in again to grant offline access")
    fid = item.get("id", "") if isinstance(item, dict) else ""
    if not isinstance(fid, str) or not fid or len(fid) > 200:
        abort(400, "Invalid file")
    from googleapiclient.discovery import build
    from app import allowed_video
    meta = allowed_video(build("drive", "v3", credentials=credentials, cache_discovery=False), fid)
    if int(meta.get("size", 0)) <= 0:
        abort(400, "Not an uploadable video")
    title = (item.get("title") or meta["name"].rsplit(".", 1)[0]).strip()[:100]
    if not title:
        abort(400, "Title required")
    # Legacy jobs must also reserve the file; never start a second upload just
    # because its browser cookie changed. Unknown ownership fails closed.
    legacy = list(db().collection("upload_jobs").where("file_id", "==", fid).limit(100).stream())
    legacy = [s for s in legacy if s.to_dict().get("status") != "duplicate"]
    legacy.sort(key=lambda s: (bool(s.to_dict().get("video_id")), bool(s.to_dict().get("location"))), reverse=True)
    candidate = legacy[0].reference if legacy else db().collection("upload_jobs").document(hashlib.sha256((owner + ":" + fid).encode()).hexdigest())
    lock = file_lock(fid)
    record = {"owner": owner, "account_id": owner, "file_id": fid,
              "name": meta["name"], "title": title, "size": int(meta["size"]), "mime_type": meta["mimeType"],
              "description": str(item.get("description", ""))[:5000],
              "tags": [str(t)[:100] for t in item.get("tags", [])][:20],
              "status": "queued", "offset": 0, "location": None,
              "token": cipher().encrypt(credentials.to_json().encode()).decode(),
              "created_at": firestore.SERVER_TIMESTAMP, "updated_at": firestore.SERVER_TIMESTAMP}
    @firestore.transactional
    def reserve(transaction):
        held = lock.get(transaction=transaction).to_dict() or {}
        ref = db().collection("upload_jobs").document(held["canonical_job_id"]) if held.get("canonical_job_id") else candidate
        saved = ref.get(transaction=transaction)
        if saved.exists:
            value = saved.to_dict()
            if value.get("account_id") != owner and value.get("owner") not in (owner, session.get("sid")):
                abort(409, "This file already has an upload record; reconnect the original account")
        else:
            transaction.set(ref, record)
        if not held.get("canonical_job_id"):
            transaction.set(lock, {"canonical_job_id": ref.id})
        return ref, not saved.exists
    ref, created = reserve(db().transaction())
    if created:
        try:
            enqueue(ref.id)
        except Exception:
            ref.update({"status": "enqueue_failed", "error": "Could not schedule upload. Reconnect to retry.", "updated_at": firestore.SERVER_TIMESTAMP})
            raise
    return {"id": ref.id, "status": "queued" if created else "already_queued"}

@queue_bp.post("/api/queue")
def add_jobs():
    csrf_check()
    identity()
    payload = request.get_json(silent=True) or {}
    items = payload.get("items", [])
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        abort(400, "Select 1–100 videos")
    from app import require_creds
    credentials = require_creds()
    created = [queue_video(credentials, item) for item in items]
    return jsonify({"jobs": created}), 202

@queue_bp.get("/api/queue")
def list_jobs():
    owner = identity()
    docs = db().collection("upload_jobs").where("account_id", "==", owner).limit(100).stream()
    items = []
    for doc in docs:
        v = doc.to_dict()
        if v.get("status") == "duplicate":
            continue
        items.append({"id": doc.id, "name": v.get("name"), "status": v.get("status"),
                      "offset": v.get("offset", 0), "size": v.get("size", 0),
                      "video_id": v.get("video_id"), "error": v.get("error"),
                      "privacy_status": v.get("privacy_status"),
                      "next_retry_at": v["next_retry_at"].isoformat() if v.get("next_retry_at") else None})
    return jsonify({"jobs": items})

def reconnect_jobs(account_id, credentials):
    """Reconnect only this verified Google account; never reset upload sessions."""
    encrypted = cipher().encrypt(credentials.to_json().encode()).decode()
    for snap in db().collection("upload_jobs").where("account_id", "==", account_id).limit(100).stream():
        job = snap.to_dict()
        if job.get("status") == "duplicate" or (job.get("status") == "completed" and job.get("privacy_verified_at")):
            continue
        if job.get("status") in ("failed", "review_required"):
            continue
        patch = {"token": encrypted, "updated_at": firestore.SERVER_TIMESTAMP}
        if job.get("status") in ("auth_required", "uploaded_unverified", "enqueue_failed"):
            patch.update({"status": "verifying" if job.get("video_id") else "queued", "error": None})
        snap.reference.update(patch)
        try:
            enqueue(snap.id, job.get("next_retry_at") if job.get("status") == "waiting_for_limit" else None)
        except Exception:
            snap.reference.update({"error": "Reconnected; upload scheduling failed. Please reconnect again."})

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

RUNNABLE = {"queued", "uploading", "retrying", "waiting_for_limit", "verifying", "uploaded_unverified", "completed"}

class UploadProblem(Exception):
    def __init__(self, reason, message, status=400):
        self.reason, self.message, self.status = reason, message, status
        super().__init__(reason)

def problem_from(exc):
    if isinstance(exc, UploadProblem):
        return exc
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            error = response.json().get("error", {})
            reason = next(iter(error.get("errors", [])), {}).get("reason", "http_error")
            message = re.sub(r"https?://\S+", "[URL]", error.get("message", "Google request failed"))[:300]
            return UploadProblem(reason, message, response.status_code)
        except (ValueError, AttributeError, TypeError):
            return UploadProblem("http_error", "Google request failed", response.status_code)
    if isinstance(exc, RefreshError):
        return UploadProblem("authorization_required", "Google authorization expired. Sign in again.", 401)
    if isinstance(exc, HTTPException):
        return UploadProblem("folder_or_request_rejected", "The approved-folder check or upload request was rejected.", exc.code)
    return UploadProblem("temporary_failure", "The worker was interrupted. The existing upload session is retained.", 503)

def claim_job(jid):
    ref = db().collection("upload_jobs").document(jid)
    lease = secrets.token_urlsafe(24)
    @firestore.transactional
    def claim(transaction):
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            return None, None
        job = snap.to_dict()
        if job.get("status") not in RUNNABLE or (job.get("status") == "completed" and job.get("privacy_verified_at")):
            return None, None
        lock = file_lock(job["file_id"])
        held = lock.get(transaction=transaction).to_dict() or {}
        canonical = held.get("canonical_job_id", jid)
        if canonical != jid:
            transaction.update(ref, {"status": "duplicate", "duplicate_of": canonical, "updated_at": firestore.SERVER_TIMESTAMP})
            return None, None
        if held.get("lease_until") and held["lease_until"] > now():
            return None, "busy"
        transaction.set(lock, {"canonical_job_id": jid, "lease_token": lease,
                              "lease_until": now() + datetime.timedelta(minutes=20)}, merge=True)
        transaction.update(ref, {"last_attempt_at": firestore.SERVER_TIMESTAMP, "updated_at": firestore.SERVER_TIMESTAMP})
        return job, lock
    job, lock = claim(db().transaction())
    return ref, job, lock, lease

def release_job(lock, lease):
    @firestore.transactional
    def release(transaction):
        held = lock.get(transaction=transaction).to_dict() or {}
        if held.get("lease_token") == lease:
            transaction.update(lock, {"lease_token": None, "lease_until": None})
    release(db().transaction())

def defer_for_limit(ref, job, when=None):
    count = int(job.get("limit_deferrals", 0)) + 1
    if count > 28:
        ref.update({"status": "review_required", "error": "YouTube's upload limit is still active after seven days. Reconnect to review.", "next_retry_at": None, "updated_at": firestore.SERVER_TIMESTAMP})
        return "", 204
    when = when or now() + datetime.timedelta(hours=6)
    # Create the continuation before acknowledging this delivery. A creation
    # failure is retried by Cloud Tasks; it must not silently strand the job.
    ref.update({"status": "waiting_for_limit", "error": "YouTube's channel upload limit is reached. Waiting to retry privately.",
                "error_reason": "uploadLimitExceeded", "next_retry_at": when,
                "limit_deferrals": count, "updated_at": firestore.SERVER_TIMESTAMP})
    enqueue(ref.id, when)
    return "", 204

def verify_video(ref, job, token):
    response = requests.get("https://www.googleapis.com/youtube/v3/videos", params={"part": "status,snippet", "id": job["video_id"]},
                            headers={"Authorization": "Bearer " + token()}, timeout=60)
    if response.status_code == 403:
        problem = problem_from(requests.HTTPError(response=response))
        if problem.reason == "insufficientPermissions":
            ref.update({"status": "uploaded_unverified", "error": "Uploaded. Sign in again to allow verification of private visibility.", "updated_at": firestore.SERVER_TIMESTAMP})
            return "", 204
    response.raise_for_status()
    items = response.json().get("items", [])
    if not items:
        raise UploadProblem("video_not_found", "YouTube did not return this existing video. Review it before any new upload.")
    status = items[0].get("status", {})
    patch = {"privacy_status": status.get("privacyStatus"), "youtube_upload_status": status.get("uploadStatus"), "updated_at": firestore.SERVER_TIMESTAMP}
    if status.get("privacyStatus") != "private" or status.get("uploadStatus") in ("failed", "rejected", "deleted"):
        patch.update({"status": "review_required", "error": "YouTube requires review: visibility or processing did not match a successful private upload."})
    else:
        patch.update({"status": "completed", "offset": int(job["size"]), "privacy_verified_at": firestore.SERVER_TIMESTAMP, "error": None, "error_reason": None, "next_retry_at": None})
    ref.update(patch)
    return "", 204

def finish_upload(ref, job, response, token):
    data = response.json()
    if not data.get("id"):
        raise UploadProblem("missing_video_id", "YouTube's upload response needs review; no new upload will be started.")
    job["video_id"] = data["id"]
    ref.update({"video_id": data["id"], "offset": int(job["size"]), "status": "verifying", "next_retry_at": None,
                "privacy_status": data.get("status", {}).get("privacyStatus"), "updated_at": firestore.SERVER_TIMESTAMP})
    file_lock(job["file_id"]).set({"video_id": data["id"]}, merge=True)
    return verify_video(ref, job, token)

@queue_bp.post("/tasks/upload")
def run_upload():
    verify_task()
    payload = request.get_json(silent=True) or {}
    jid = payload.get("job_id")
    if not isinstance(jid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", jid):
        abort(400)
    ref, job, lock, lease = claim_job(jid)
    if lock == "busy":
        return "Upload already running", 503
    if job is None:
        return "", 204
    try:
        return perform_upload(ref, job, jid)
    except Exception as exc:
        problem = problem_from(exc)
        current_app.logger.warning("upload_job=%s reason=%s http_status=%s", jid, problem.reason, problem.status)
        if problem.reason == "uploadLimitExceeded":
            when = now() + datetime.timedelta(hours=6)
            db().collection("upload_channels").document(job["account_id"]).set({"blocked_until": when})
            try:
                return defer_for_limit(ref, job, when)
            except Exception:
                return "Retry scheduling failed", 503
        if problem.status in (401, 403) or problem.reason == "authorization_required":
            state = "auth_required"
        elif problem.status in (404, 410) and job.get("location"):
            state = "review_required"
            problem.message = "The existing upload session expired. Review YouTube before restarting to avoid a duplicate."
        elif problem.status >= 500 or problem.status == 429:
            state = "retrying"
        else:
            state = "review_required"
        ref.update({"status": state, "error": problem.message, "error_reason": problem.reason, "updated_at": firestore.SERVER_TIMESTAMP})
        return ("Temporary upload failure", 503) if state == "retrying" else ("", 204)
    finally:
        release_job(lock, lease)

def perform_upload(ref, job, jid):
    info = json.loads(cipher().decrypt(job["token"].encode()))
    c = Credentials.from_authorized_user_info(info, scopes=info.get("scopes"))
    def token():
        if not c.valid or c.expired:
            c.refresh(Request())
            ref.update({"token": cipher().encrypt(c.to_json().encode()).decode()})
        return c.token
    # A saved video ID always means verify-only, even after an interrupted
    # final response or renewed authorization. Never initiate it again.
    if job.get("video_id"):
        return verify_video(ref, job, token)
    # Re-check folder membership when a task runs, including after a folder move.
    from googleapiclient.discovery import build
    from app import allowed_video
    allowed_video(build("drive", "v3", credentials=c, cache_discovery=False), job["file_id"])
    paused = db().collection("upload_channels").document(job["account_id"]).get().to_dict() or {}
    if paused.get("blocked_until") and paused["blocked_until"] > now():
        return defer_for_limit(ref, job, paused["blocked_until"])
    size = int(job["size"])
    offset = int(job.get("offset") or 0)
    location = job.get("location")
    if not location:
        metadata = {"snippet": {"title": job["title"], "categoryId": "10", "description": job.get("description", ""), "tags": job.get("tags", [])},
                    "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False}}
        r = requests.post("https://www.googleapis.com/upload/youtube/v3/videos",
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={"Authorization": "Bearer " + token(), "X-Upload-Content-Type": job["mime_type"],
                     "X-Upload-Content-Length": str(size)},
            json=metadata, timeout=60)
        r.raise_for_status()
        location = r.headers["Location"]
        host = urlparse(location).hostname or ""
        if urlparse(location).scheme != "https" or host not in ("www.googleapis.com", "youtube.googleapis.com"):
            abort(502, "Invalid resumable URL")
        ref.update({"location": location, "status": "uploading", "updated_at": firestore.SERVER_TIMESTAMP})
    else:
        if urlparse(location).scheme != "https" or urlparse(location).hostname not in ("www.googleapis.com", "youtube.googleapis.com"):
            raise UploadProblem("invalid_session_url", "The saved upload endpoint needs review.")
        # Query server's committed offset after a retry / interrupted worker.
        r = requests.put(location, headers={"Content-Length": "0",
                         "Content-Range": "bytes */" + str(size),
                         "Authorization": "Bearer " + token()}, timeout=60)
        if r.status_code in (200, 201):
            return finish_upload(ref, job, r, token)
        if r.status_code == 308:
            rng = r.headers.get("Range", "")
            offset = int(rng.rsplit("-", 1)[-1]) + 1 if "-" in rng else 0
        else:
            r.raise_for_status()
            abort(502)
    # Keep each dispatch below the HTTP deadline, then enqueue continuation.
    deadline = time.monotonic() + 3 * 60
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
            return finish_upload(ref, job, resp, token)
        ref.update(update)
    # A 308 is not proof of completion, even if its offset reaches file size.
    raise UploadProblem("awaiting_completion", "Waiting for YouTube to confirm the saved upload.", 503)

