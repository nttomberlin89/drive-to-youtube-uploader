# Durable background uploader setup

This branch requires Google Cloud infrastructure **before deployment**. Do not merge it into the Cloud Build deployment branch until the following are provisioned.

1. Enable APIs: Cloud Tasks, Firestore, Cloud Run, IAM Credentials.
2. Create a Firestore Native database in the same Google Cloud project.
3. Create a Cloud Tasks queue in us-central1 with max concurrent dispatches 1 and a retry policy with backoff. Each worker dispatch has a 900-second deadline and schedules a continuation after approximately three minutes of transfer. Set the Cloud Run request timeout to 900 seconds and concurrency to 8 to match the threaded Gunicorn process.
4. Create a task-invoker service account. Grant it Cloud Run Invoker on the uploader service. Grant the Cloud Run runtime service account Cloud Tasks Enqueuer on the queue, Cloud Datastore User on the Firestore database, and Service Account User on the task-invoker account. The Cloud Tasks service agent needs its standard Cloud Tasks Service Agent role to mint dispatch tokens.
5. Generate a Fernet key **privately** using `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and store it in Secret Manager. Never commit or paste it in chat.
6. Set Cloud Run environment variables:
   - `GCP_PROJECT=ninth-rhino-513318-h4`
   - `TASKS_LOCATION=us-central1`
   - `TASKS_QUEUE=<queue name>`
   - `TASKS_SERVICE_ACCOUNT=<invoker service account email>`
   - `TASKS_TARGET_URL=https://drive-to-youtube-uploader-1025716865447.us-central1.run.app`
   - `TOKEN_ENCRYPTION_KEY` from Secret Manager
7. Keep existing `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `FLASK_SECRET_KEY`, `ALLOWED_EMAIL` settings. Sign in again to obtain a refresh token.
8. Deploy and test with one small private video. Confirm queue status after closing Safari and opening it again.

## Important limitations

- Individual and batch uploads use the same background queue and transactional per-file reservation. Sign-ins are linked to Google's stable account ID. Workers take a per-file lease and retain the same resumable session across retries.
- YouTube's `uploadLimitExceeded` is a channel upload cap, not a Cloud IAM failure. Jobs show `waiting_for_limit`, share a channel cooldown, and schedule another attempt in six hours. After 28 deferrals, they stop for review. Existing video IDs are only verified, never re-uploaded.
- Privacy is checked using `videos.list` after upload. Existing connections must consent again to the additional `youtube.readonly` scope. The OAuth callback reconnects this account's jobs without clearing upload offsets, sessions, or video IDs.
- OAuth consent apps in Testing mode can have refresh tokens that expire after 7 days. Publishing/verification may be needed for ongoing operation.
- Expired upload sessions stop for review rather than starting another video automatically. Authorization errors appear in the saved queue and can be resumed by reconnecting Google.
- Firestore rules and indexes must be configured; all access here uses server-side IAM, not client-side Firestore.
- Store encryption keys in Secret Manager and use dedicated IAM service accounts. Do not deploy without required secrets.
- YouTube API upload quota and API-project verification can limit publishing.

Run `python -m unittest -v test_queue` before deployment. When upgrading legacy browser-session-owned jobs, verify the Google identity behind each encrypted token, attach `account_id`, and reserve one canonical job per file in `upload_locks`. Mark redundant records `duplicate` rather than deleting them. Never discard a saved upload session, offset, or video ID during recovery.

