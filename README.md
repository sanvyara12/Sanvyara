# Sanvyara AI (simple version)

Only 2 real files:

| File | What it is |
|------|------------|
| `index.html` | The whole frontend (login, Ask AI, Library, Admin) - no build step |
| `server.py`  | The whole backend (FastAPI). Database = your Google Sheet |

## Run locally
```bash
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn server:app --reload --port 8000
```
Open http://localhost:8000

With no `.env` it runs in **demo mode** (temporary in-memory data):
admin `admin@sanvyara.ai / admin123`, student `rahul@example.com / student123`.

## Connect Google Sheet (real database)
1. Create an empty Google Sheet.
2. Google Cloud Console -> create a **Service Account** -> Keys -> add JSON key. Enable the **Google Sheets API** and **Google Drive API** for the project.
3. Share the Sheet with the service account's `client_email` as **Editor**.
4. Copy `.env.example` to `.env` and fill `GOOGLE_SHEET_URL`, `GOOGLE_SERVICE_ACCOUNT_JSON` (one line), `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `SECRET_KEY`.
5. Start the server. It creates these tabs itself: `Users, Courses, Semesters, Subjects, Documents, Chats`, and creates the admin login.

## Deploy on Render (one service = frontend + backend)
- New -> Web Service -> connect repo
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn server:app --host 0.0.0.0 --port $PORT`
- Add the same variables as `.env` in Render -> Environment

## Notes
- Students register themselves; only the admin account can add courses / semesters / subjects.
- Documents are Google Drive **links** (PDF file upload was removed - Render's disk is temporary).
- AI answers use a small built-in demo knowledge base (DBMS, OS). Reading the real text of your PDFs is not implemented yet.
