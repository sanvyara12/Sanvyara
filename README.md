# Sanvyara AI

| File | Purpose |
|------|---------|
| `index.html` | Whole frontend (login, Ask AI, Library, Admin) |
| `server.py` | Whole backend (FastAPI). Database = Google Sheet |
| `drive_upload.gs` | Small Google Apps Script that saves uploads into your Drive |

## Run
```bash
pip install -r requirements.txt
cp .env.example .env      # fill the values below
uvicorn server:app --reload --port 8000
```
Open http://localhost:8000. Render start command: `uvicorn server:app --host 0.0.0.0 --port $PORT` (same variables in Render -> Environment).

## 1. Connect the Google Sheet (database) - EASY way, no Google Cloud
Use the same Apps Script from step 2 as the database:
1. Open your Google Sheet, copy its ID (the part after `/spreadsheets/d/`).
2. In `drive_upload.gs` set `SHEET_ID` (plus `FOLDER_ID` and `KEY`), then deploy it (step 2).
3. Set `DRIVE_UPLOAD_URL` and `DRIVE_UPLOAD_KEY` on the server. Nothing else is needed.
4. Start the server. The log must say `[DB] Connected to Google Sheet through Apps Script`.

Tabs `Users, Courses, Semesters, Subjects, Documents, Knowledge, Chats` are created automatically, and the admin from `ADMIN_EMAIL` / `ADMIN_PASSWORD`.

(Old way still works: service account via `GOOGLE_SERVICE_ACCOUNT_JSON` + `GOOGLE_SHEET_URL`. If both are set, the service account is used.)

## 2. Upload to Google Drive
1. Make a folder in Drive; copy its ID (the part after `/folders/`).
2. script.google.com -> New project -> paste `drive_upload.gs`, set `FOLDER_ID` and `KEY`.
3. Deploy -> New deployment -> Web app -> Execute as **Me**, access **Anyone** -> copy the URL.
4. Set `DRIVE_UPLOAD_URL` (that URL) and `DRIVE_UPLOAD_KEY` (same KEY).

Uploaded files go to your folder; the link is saved in the Sheet (`Documents` tab).

## 3. AI answers (NO API key needed)
Answers come only from **your own data in Google Drive**. Ways to feed it:
1. Library -> upload a PDF / Word / PPT / TXT: saved to your Drive folder, its text is read and saved to the `Knowledge` tab.
1b. Library -> paste a Drive link, or Admin -> **Sync Drive files for AI** for older files: the server reads them through `drive_upload.gs`. **Re-deploy the script (new version) after updating it** - it now has a `read` action.
2. Admin -> **Add knowledge for AI**: paste notes / Q&A text for a subject.

Ask AI searches that text and builds the Answer / Summary / Notes / Exam answer / MCQs from the best matching parts. If nothing matches it says so. Scanned (image-only) PDFs have no text - paste the text via Admin instead.
Every answer ends with **Design & Developed by Mr. Rohit**, and the footer is on every page.
