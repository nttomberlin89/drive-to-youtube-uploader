# Drive to YouTube Uploader

A mobile-friendly Flask uploader that reads Google Drive videos and uploads them to YouTube as private videos. Google OAuth restricts access to ALLOWED_EMAIL.

## Cloud Run environment variables
- GOOGLE_CLIENT_ID: OAuth web client ID
- GOOGLE_CLIENT_SECRET: OAuth web client secret (use Secret Manager)
- FLASK_SECRET_KEY: long random value (use Secret Manager)
- ALLOWED_EMAIL: exact Google account email allowed to use this app

Add the Cloud Run URL plus /callback as an authorized redirect URI in Google Auth Platform > Clients. Enable Google Drive API and YouTube Data API v3 in the correct project. Configure OAuth consent audience and test user.

Buildpacks support Python from requirements.txt and Procfile. Cloud Run should run **one instance** for this MVP, because OAuth tokens are stored in process memory and are lost on restart. Do not put secrets in GitHub. Set a budget alert and inspect Cloud Run billing.

**Limitations:** MVP uploads synchronously, so keep the browser open; large files may time out or fail, and retries are not yet implemented. Only first 100 Drive videos shown. All uploads are private; YouTube may impose additional private-only restrictions on unverified API projects. Review YouTube's API audit requirements before publishing publicly. Add durable upload queue, encrypted token storage, and upload recovery before production use.
