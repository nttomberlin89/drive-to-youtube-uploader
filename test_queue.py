import concurrent.futures
import copy
import datetime
import json
import os
import threading
import unittest
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
import app
import background_queue as queue
import requests

class Snapshot:
    def __init__(self, ref):
        self.reference, self.id = ref, ref.id
        self.value = copy.deepcopy(ref.database.data.get(ref.key))
        self.exists = self.value is not None
    def to_dict(self):
        return self.value

class Ref:
    def __init__(self, database, collection, ident):
        self.database, self.collection, self.id = database, collection, ident
        self.key = (collection, ident)
    def get(self, transaction=None):
        return Snapshot(self)
    def set(self, value, merge=False):
        if merge:
            self.database.data.setdefault(self.key, {}).update(value)
        else:
            self.database.data[self.key] = dict(value)
    def update(self, value):
        self.database.data[self.key].update(value)

class Collection:
    def __init__(self, database, name, conditions=()):
        self.database, self.name, self.conditions = database, name, conditions
    def document(self, ident):
        return Ref(self.database, self.name, ident)
    def where(self, field, op, value):
        assert op == "=="
        return Collection(self.database, self.name, (*self.conditions, (field, value)))
    def limit(self, limit):
        return self
    def stream(self):
        for (name, ident), value in list(self.database.data.items()):
            if name == self.name and all(value.get(k) == v for k, v in self.conditions):
                yield self.document(ident).get()

class Database:
    def __init__(self):
        self.data = {}
        self.lock = threading.RLock()
    def collection(self, name):
        return Collection(self, name)
    def transaction(self):
        return self
    def set(self, ref, value, merge=False):
        ref.set(value, merge)
    def update(self, ref, value):
        ref.update(value)

def transactional(fn):
    def wrapped(transaction):
        with transaction.lock:
            return fn(transaction)
    return wrapped

def response(status, data=None, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(data or {}).encode()
    result.headers.update(headers or {})
    return result

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.meta = {"id":"file-1", "name":"video.mp4", "size":"16", "mimeType":"video/mp4"}
        self.credentials = Mock(refresh_token="refresh", valid=True, expired=False, token="access")
        self.credentials.to_json.return_value = json.dumps({"refresh_token":"refresh", "client_id":"client", "client_secret":"secret"})
        self.patches = [patch.object(queue, "_db", self.database), patch.object(queue.firestore, "transactional", transactional),
                        patch.object(app, "account_identity", return_value="account-1"), patch.object(app, "allowed_video", return_value=self.meta),
                        patch("googleapiclient.discovery.build"), patch.object(queue, "enqueue"),
                        patch.object(queue.Credentials, "from_authorized_user_info", return_value=self.credentials), patch.object(queue, "verify_task")]
        self.mocks = [p.start() for p in self.patches]
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])
        self.enqueue = self.mocks[5]
    def create(self, sid="browser-1"):
        with app.app.test_request_context():
            app.session["sid"] = sid
            return queue.queue_video(self.credentials, {"id":"file-1"})
    def job(self):
        result = self.create()
        ref = self.database.collection("upload_jobs").document(result["id"])
        self.enqueue.reset_mock()
        return ref
    def test_signing_in_again_does_not_create_another_upload(self):
        one, two = self.create(), self.create("browser-2")
        self.assertEqual(one["id"], two["id"])
        self.enqueue.assert_called_once()
    def test_concurrent_queue_requests_reserve_one_job(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(lambda i: self.create(str(i)), range(16)))
        self.assertEqual(len({v["id"] for v in values}), 1)
        self.enqueue.assert_called_once()
    def test_legacy_completed_record_is_reused(self):
        self.database.collection("upload_jobs").document("legacy").set({"file_id":"file-1", "account_id":"account-1", "video_id":"existing", "status":"completed"})
        self.assertEqual(self.create()["id"], "legacy")
        self.enqueue.assert_not_called()
    def test_worker_lock_blocks_a_second_concurrent_worker(self):
        ref = self.job()
        _, first, lock, lease = queue.claim_job(ref.id)
        _, second, busy, _ = queue.claim_job(ref.id)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(busy, "busy")
        queue.release_job(lock, lease)
        self.assertIsNotNone(queue.claim_job(ref.id)[1])
    def test_reconnect_preserves_deferred_job_without_extra_task(self):
        ref=self.job()
        ref.update({"status":"waiting_for_limit","next_retry_at":queue.now()+datetime.timedelta(hours=6)})
        queue.reconnect_jobs("account-1",self.credentials)
        self.enqueue.assert_not_called()
        self.assertEqual(ref.get().to_dict()["status"],"waiting_for_limit")
    def test_reconnect_resumes_verification_without_clearing_video(self):
        ref=self.job()
        ref.update({"status":"uploaded_unverified","video_id":"existing","location":"https://www.googleapis.com/saved"})
        queue.reconnect_jobs("account-1",self.credentials)
        self.enqueue.assert_called_once_with(ref.id)
        self.assertEqual(ref.get().to_dict()["video_id"],"existing")
        self.assertEqual(ref.get().to_dict()["status"],"verifying")
    def test_alias_worker_never_uploads(self):
        ref = self.job()
        alias = self.database.collection("upload_jobs").document("alias")
        alias.set(dict(ref.get().to_dict()))
        self.assertIsNone(queue.claim_job(alias.id)[1])
        self.assertEqual(alias.get().to_dict()["status"], "duplicate")
    def test_upload_limit_is_recorded_and_scheduled_without_new_session(self):
        ref = self.job()
        error = response(400, {"error":{"message":"Upload limit reached", "errors":[{"reason":"uploadLimitExceeded"}]}})
        with patch.object(queue.requests, "post", return_value=error) as post:
            result = app.app.test_client().post("/tasks/upload", json={"job_id":ref.id})
        self.assertEqual(result.status_code, 204)
        saved = ref.get().to_dict()
        self.assertEqual(saved["status"], "waiting_for_limit")
        self.assertEqual(saved["error_reason"], "uploadLimitExceeded")
        self.assertGreater(saved["next_retry_at"], queue.now() + datetime.timedelta(hours=5))
        self.assertIsNone(saved["location"])
        self.enqueue.assert_called_once()
        self.assertEqual(post.call_args.kwargs["json"]["status"]["privacyStatus"], "private")
    def test_shared_channel_limit_does_not_probe_youtube_again(self):
        ref = self.job()
        self.database.collection("upload_channels").document("account-1").set({"blocked_until":queue.now()+datetime.timedelta(hours=5)})
        with patch.object(queue.requests, "post") as post:
            result = app.app.test_client().post("/tasks/upload", json={"job_id":ref.id})
        self.assertEqual(result.status_code, 204)
        post.assert_not_called()
        self.enqueue.assert_called_once()
    def test_saved_video_id_only_verifies_and_never_starts_upload(self):
        ref = self.job()
        ref.update({"video_id":"existing", "status":"verifying"})
        with patch.object(queue.requests, "get", return_value=response(200,{"items":[{"status":{"privacyStatus":"private","uploadStatus":"processed"}}]})), patch.object(queue.requests, "post") as post:
            result = app.app.test_client().post("/tasks/upload", json={"job_id":ref.id})
        self.assertEqual(result.status_code, 204)
        self.assertEqual(ref.get().to_dict()["status"], "completed")
        post.assert_not_called()
    def test_missing_read_scope_keeps_id_and_waits_for_consent(self):
        ref = self.job()
        ref.update({"video_id":"existing", "status":"verifying"})
        with patch.object(queue.requests, "get", return_value=response(403,{"error":{"errors":[{"reason":"insufficientPermissions"}]}})):
            result = app.app.test_client().post("/tasks/upload", json={"job_id":ref.id})
        self.assertEqual(result.status_code, 204)
        self.assertEqual(ref.get().to_dict()["status"], "uploaded_unverified")
        self.assertEqual(ref.get().to_dict()["video_id"], "existing")
    def test_private_visibility_without_upload_confirmation_is_not_completed(self):
        ref = self.job()
        ref.update({"video_id":"existing", "status":"verifying"})
        with patch.object(queue.requests, "get", return_value=response(200,{"items":[{"status":{"privacyStatus":"private"}}]})):
            result = app.app.test_client().post("/tasks/upload", json={"job_id":ref.id})
        self.assertEqual(result.status_code, 204)
        self.assertEqual(ref.get().to_dict()["status"], "review_required")
    def test_expired_resumable_session_requires_review_not_reupload(self):
        ref = self.job()
        ref.update({"location":"https://www.googleapis.com/upload/youtube/v3/videos?upload_id=saved", "status":"uploading"})
        with patch.object(queue.requests, "put", return_value=response(404)), patch.object(queue.requests, "post") as post:
            result = app.app.test_client().post("/tasks/upload", json={"job_id":ref.id})
        self.assertEqual(result.status_code, 204)
        self.assertEqual(ref.get().to_dict()["status"], "review_required")
        post.assert_not_called()
    def test_outside_drums_file_is_denied(self):
        # Use the original folder guard, not the fixture mock.
        self.patches[3].stop()
        drive = Mock()
        drive.files.return_value.get.return_value.execute.side_effect = [{"mimeType":"video/mp4","parents":["outside"]},{"parents":[]}]
        with app.app.test_request_context():
            with self.assertRaises(Exception) as raised:
                app.allowed_video(drive, "outside-video")
        self.assertEqual(raised.exception.code, 404)

class TaskPayloadTests(unittest.TestCase):
    def test_repeated_deferred_task_is_deduplicated(self):
        client=Mock()
        client.queue_path.return_value="projects/project/locations/region/queues/queue"
        config={"GCP_PROJECT":"project","TASKS_LOCATION":"region","TASKS_QUEUE":"queue","TASKS_TARGET_URL":"https://service.run.app","TASKS_SERVICE_ACCOUNT":"worker@example.com"}
        when=queue.now()+datetime.timedelta(hours=6)
        with patch.dict(os.environ,config), patch.object(queue,"task_client",return_value=client):
            queue.enqueue("same-job",when)
            first=client.create_task.call_args.kwargs["task"]
            client.create_task.side_effect=queue.AlreadyExists("same task")
            queue.enqueue("same-job",when)
            second=client.create_task.call_args.kwargs["task"]
        self.assertEqual(first["name"],second["name"])
        self.assertEqual(first["schedule_time"],when)

if __name__ == "__main__":
    unittest.main()
