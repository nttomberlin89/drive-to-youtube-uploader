# Durable background uploader setup

This branch requires Google Cloud infrastructure **before deployment**. Do not merge it into the Cloud Build deployment branch until the following are provisioned.

1. Enable APIs: Cloud Tasks, Firestore, Cloud Run, IAM Credentials.
2. Create a Firestore Native database in the same Google Cloud project.
3. Create a Cloud Tasks queue in us-central1 with HTTP dispatch deadline 1800 seconds, max concurrent dispatches 1, and retry policy. For large videos exceeding 30 minutes, split work into continuation tasks (not yet implemented).
4. Create a task-invoker service account. Grant it Cloud Run Invoker on the uploader service. Grant the Cloud Run runtime service account Cloud Tasks Enqueuer on the queue and Cloud Datastore User on the Firestore database. Grant the runtime service account permission to mint OIDC tokens for the task-invoker service account (Service Account Token Creator).
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

- The existing one-at-a-time upload endpoint still runs in the browser. Only the gallery's **Upload selected privately** action uses the background queue.
- Cloud Tasks HTTP tasks have a maximum dispatch deadline of 30 minutes. A single large video may exceed that. Continuation scheduling is needed for production-grade multi-hour transfers.
- OAuth consent apps in Testing mode can have refresh tokens that expire after 7 days. Publishing/verification may be needed for ongoing operation.
- The queue is resumable on task retry using YouTube's upload session URI; upload session expiration and token revocation still need explicit recovery UX.
- Firestore rules and indexes must be configured; all access here uses server-side IAM, not client-side Firestore.
- Store encryption keys in Secret Manager and use dedicated IAM service accounts. Do not deploy without required secrets.
- YouTube API upload quota and API-project verification can limit publishing.
