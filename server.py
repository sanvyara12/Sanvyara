"""
Sanvyara AI - single-file backend.

  * Serves index.html (the whole frontend)
  * JSON API under /api/*
  * Database = Google Sheet (one tab per table). If no Sheet is configured
    it falls back to in-memory demo data so you can try it instantly.
  * NO AI KEY NEEDED: answers are built only from YOUR uploaded notes/text
    (stored in the "Knowledge" tab). Footer: Design & Developed by Mr. Rohit.

Run:  uvicorn server:app --reload --port 8000
"""
import base64
import hashlib
import io
import math
import random
from collections import Counter
import hmac
import json
import mimetypes
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import bcrypt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

load_dotenv()

BASE = Path(__file__).parent
SECRET = os.getenv("SECRET_KEY", "change-this-secret")
TOKEN_DAYS = 7
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

if SECRET == "change-this-secret":
    print("[WARN] SECRET_KEY not set - set a long random value in production.")

# --------------------------------------------------------------------------
# Database (Google Sheet)  -  every tab is one table
# --------------------------------------------------------------------------
TABS = {
    "Users": ["user_id", "name", "email", "password_hash", "role", "status", "created_at"],
    "Courses": ["course_id", "course_name"],
    "Semesters": ["semester_id", "course_id", "semester_name"],
    "Subjects": ["subject_id", "semester_id", "subject_code", "subject_name"],
    "Documents": ["document_id", "subject_id", "pdf_name", "drive_link", "document_type",
                  "uploaded_by", "uploaded_at"],
    "Knowledge": ["chunk_id", "document_id", "subject_id", "title", "text"],
    "Chats": ["message_id", "user_id", "mode", "question", "answer", "created_at"],
}


def new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:8].upper()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sheet_id() -> str:
    raw = os.getenv("GOOGLE_SHEET_ID") or os.getenv("GOOGLE_SHEET_URL") or ""
    m = re.search(r"/spreadsheets/d/([\w-]+)", raw)
    return m.group(1) if m else raw.strip()


def load_sa(raw: str) -> dict:
    """Service-account JSON: plain, with pasted line breaks, or base64."""
    raw = raw.strip()
    try:
        return json.loads(raw, strict=False)
    except ValueError:
        return json.loads(base64.b64decode(raw))


class ScriptDBError(Exception):
    pass


class Store:
    """Database = Google Sheet. Two ways to reach it (no Google Cloud needed for the 2nd):
       1) service account (GOOGLE_SERVICE_ACCOUNT_JSON + GOOGLE_SHEET_URL)  - gspread
       2) your Apps Script web app (DRIVE_UPLOAD_URL + DRIVE_UPLOAD_KEY)   - same script that does Drive
       If neither is set it keeps data in memory (demo; resets on restart)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}   # tab -> (time, rows)   (avoids hitting Google's read quota)
        self.ws = {}      # tab -> worksheet
        self.cols = {}    # tab -> header row
        self.mem = None   # dict of lists when running without a Sheet
        self.script = None  # Apps Script URL when using way (2)
        self.hint = ""    # why the Sheet is not connected (shown in the app)

        info = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        keyfile = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
        try:
            if (info or keyfile) and sheet_id():
                self._connect_gspread(info, keyfile)
            elif os.getenv("DRIVE_UPLOAD_URL"):
                self._connect_script(os.environ["DRIVE_UPLOAD_URL"])
            else:
                raise RuntimeError("not set: add DRIVE_UPLOAD_URL + DRIVE_UPLOAD_KEY (Apps Script) in server settings")
        except Exception as e:
            print(f"[DB] Google Sheet NOT connected: {type(e).__name__}: {e}")
            kind = type(e).__name__
            self.hint = (str(e) if kind == "ScriptDBError" or "not set" in str(e)
                         else "Service-account key is not valid JSON" if kind in ("JSONDecodeError", "ValueError", "KeyError")
                         else "Google refused access - share the Sheet with the service-account email (Editor) "
                              "and enable Google Sheets API + Drive API" if kind in ("APIError", "SpreadsheetNotFound", "PermissionError")
                         else "Could not connect: " + kind)
            print("[DB] Using in-memory demo data - it resets on restart.")
            self.mem = {t: [] for t in TABS}

    # ---- way 1: service account ----
    def _connect_gspread(self, info, keyfile):
        import gspread
        if info:
            sa = load_sa(info)
            gc = gspread.service_account_from_dict(sa)
        else:
            sa = json.load(open(keyfile))
            gc = gspread.service_account(filename=keyfile)
        print("[DB] Sheet must be shared (Editor) with:", sa.get("client_email"))
        book = gc.open_by_key(sheet_id())
        have = {w.title: w for w in book.worksheets()}
        for tab, cols in TABS.items():
            w = have.get(tab) or book.add_worksheet(tab, rows=1000, cols=len(cols))
            head = w.row_values(1)
            if not head:
                w.append_row(cols)
                head = cols
            self.ws[tab] = w
            self.cols[tab] = head
        print(f"[DB] Connected to Google Sheet: {book.title}")

    # ---- way 2: Apps Script ----
    def _call(self, **payload) -> dict:
        import httpx
        try:
            r = httpx.post(self.script, follow_redirects=True, timeout=60,
                           json={"key": os.getenv("DRIVE_UPLOAD_KEY", ""), **payload})
            r.raise_for_status()
            out = r.json()
        except Exception as e:
            raise ScriptDBError("Apps Script not reachable - deploy it as Web app (Execute as Me, access Anyone) "
                                f"and use the /exec URL [{type(e).__name__}]")
        if out.get("error"):
            raise ScriptDBError("Apps Script says: " + str(out["error"]) +
                                " (re-deploy drive_upload.gs as a NEW VERSION and set SHEET_ID in it)")
        return out

    def _connect_script(self, url):
        self.script = url
        self._call(action="init", tabs=TABS)
        print("[DB] Connected to Google Sheet through Apps Script")

    def rows(self, tab: str) -> list:
        with self.lock:
            if self.mem is not None:
                return [dict(r) for r in self.mem[tab]]
            hit = self.cache.get(tab)
            if hit and time.time() - hit[0] < 10:
                return hit[1]
            if self.script:
                data = self._call(action="rows", tab=tab, cols=TABS[tab])["rows"]
            else:
                data = self.ws[tab].get_all_records(numericise_ignore=["all"])
            self.cache[tab] = (time.time(), data)
            return data

    def add(self, tab: str, row: dict) -> dict:
        self.add_many(tab, [row])
        return row

    def add_many(self, tab: str, rows: list) -> None:
        if not rows:
            return
        with self.lock:
            if self.mem is not None:
                self.mem[tab].extend(dict(r) for r in rows)
                return
            if self.script:
                self._call(action="append", tab=tab, cols=TABS[tab],
                           rows=[{c: str(r.get(c, "")) for c in TABS[tab]} for r in rows])
            else:
                # RAW => text starting with "=" is stored as text, never as a formula
                self.ws[tab].append_rows([[str(r.get(c, "")) for c in self.cols[tab]] for r in rows],
                                         value_input_option="RAW")
            self.cache.pop(tab, None)

    def delete_where(self, tab: str, col: str, val: str) -> None:
        """Delete every row where col == val (e.g. all text chunks of one document)."""
        with self.lock:
            if self.mem is not None:
                self.mem[tab] = [r for r in self.mem[tab] if str(r.get(col)) != val]
                return
            if self.script:
                self._call(action="delete", tab=tab, cols=TABS[tab], col=col, val=val)
                self.cache.pop(tab, None)
                return
            data = self.ws[tab].get_all_records(numericise_ignore=["all"])
            idx = [i for i, r in enumerate(data, start=2) if str(r.get(col)) == val]
            runs, start, prev = [], None, None          # group into contiguous runs
            for i in idx:
                if start is None:
                    start = prev = i
                elif i == prev + 1:
                    prev = i
                else:
                    runs.append((start, prev)); start = prev = i
            if start is not None:
                runs.append((start, prev))
            for a, b in reversed(runs):                  # bottom first so row numbers stay valid
                self.ws[tab].delete_rows(a, b)
            self.cache.pop(tab, None)

    def delete(self, tab: str, col: str, val: str) -> bool:
        before = len(self.rows(tab))
        self.delete_where(tab, col, val)
        return len(self.rows(tab)) < before


db = Store()

# --------------------------------------------------------------------------
# Auth (password hash + signed token, no extra libraries)
# --------------------------------------------------------------------------
def hash_pw(p: str) -> str:
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()


def check_pw(p: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(p.encode(), hashed.encode())
    except Exception:
        return False


def _sign(payload: str) -> str:
    return hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_token(user_id: str) -> str:
    payload = f"{user_id}.{int(time.time()) + TOKEN_DAYS * 86400}"
    return base64.urlsafe_b64encode(f"{payload}.{_sign(payload)}".encode()).decode()


def read_token(token: str) -> Optional[str]:
    try:
        payload, sig = base64.urlsafe_b64decode(token.encode()).decode().rsplit(".", 1)
        user_id, exp = payload.split(".")
        if hmac.compare_digest(sig, _sign(payload)) and int(exp) > time.time():
            return user_id
    except Exception:
        pass
    return None


def find_user(**match) -> Optional[dict]:
    for u in db.rows("Users"):
        if all(str(u.get(k, "")).lower() == str(v).lower() for k, v in match.items()):
            return u
    return None


def public(u: dict) -> dict:
    return {"user_id": u["user_id"], "name": u["name"], "email": u["email"], "role": u["role"]}


def current_user(authorization: Optional[str] = Header(None)) -> dict:
    uid = read_token(authorization[7:]) if authorization and authorization.startswith("Bearer ") else None
    user = find_user(user_id=uid) if uid else None
    if not user or (user.get("status") or "active") != "active":
        raise HTTPException(401, "Please log in again")
    return user


def admin_only(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Admin only")
    return user


# --------------------------------------------------------------------------
# Seed data (first run)
# --------------------------------------------------------------------------
def seed():
    if not db.rows("Users"):
        email = os.getenv("ADMIN_EMAIL", "admin@sanvyara.ai").lower()
        pw = os.getenv("ADMIN_PASSWORD", "admin123")
        db.add("Users", {"user_id": new_id("U"), "name": "Admin", "email": email,
                         "password_hash": hash_pw(pw), "role": "admin",
                         "status": "active", "created_at": now()})
        print(f"[SEED] Admin created: {email}" + ("  (default password - change it!)" if pw == "admin123" else ""))

    if db.mem is not None and not db.rows("Courses"):   # demo data only in memory mode
        db.add("Users", {"user_id": new_id("U"), "name": "Rahul Sharma", "email": "rahul@example.com",
                         "password_hash": hash_pw("student123"), "role": "student",
                         "status": "active", "created_at": now()})
        db.add("Courses", {"course_id": "C01", "course_name": "BCA"})
        db.add("Semesters", {"semester_id": "SEM4", "course_id": "C01", "semester_name": "Semester 4"})
        db.add("Subjects", {"subject_id": "SUB1", "semester_id": "SEM4", "subject_code": "BCA-401", "subject_name": "DBMS"})
        db.add("Subjects", {"subject_id": "SUB2", "semester_id": "SEM4", "subject_code": "BCA-402", "subject_name": "Operating Systems"})
        db.add("Documents", {"document_id": "D01", "subject_id": "SUB1", "pdf_name": "DBMS Unit 1 Notes",
                             "drive_link": "https://drive.google.com/", "document_type": "Notes",
                             "uploaded_by": "", "uploaded_at": now()})
        save_knowledge("D01", "SUB1", "DBMS Unit 1 Notes",
            "DBMS stands for Database Management System. A DBMS is software that stores, organizes and manages data "
            "so that users can retrieve and update it easily. Examples of DBMS are MySQL, Oracle and PostgreSQL.\n\n"
            "Normalization is the process of organizing data in a database to reduce redundancy and improve data integrity. "
            "First Normal Form (1NF) requires that every column holds atomic values. Second Normal Form (2NF) removes "
            "partial dependency on the primary key. Third Normal Form (3NF) removes transitive dependency between non-key columns.\n\n"
            "A primary key uniquely identifies each record in a table and cannot contain NULL values. "
            "A foreign key is a column that refers to the primary key of another table and links the two tables together.")


# --------------------------------------------------------------------------
# "AI" answers - NO API KEY. Answers come only from your own uploaded data
# (Knowledge tab): search the best matching passages, then format them.
# --------------------------------------------------------------------------
FOOTER = "Design & Developed by Mr. Rohit"
FOOT_TXT = "\n\n— " + FOOTER

STOP = set("""a an the is are was were be been am of in on at to for from by with and or but if then so as it its this that
these those what which who whom how why when where do does did can could should would will shall may might about into
than also very just not no yes me my we our you your he she they them his her their i
kya hai hain ho tha thi the ka ki ke ko se me mein par aur ya yeh ye wo woh kaise kyun kyu kab kahan kaun kuch
batao bataiye samjhao explain define tell give write short note notes answer question meaning example please""".split())


def tokens(text: str) -> list:
    out = []
    for w in re.findall(r"[a-zA-Z0-9\u0900-\u097F]+", text.lower()):
        if len(w) < 2 or w in STOP:
            continue
        if len(w) > 4 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


def sentences(text: str) -> list:
    parts = re.split(r"(?<=[.!?।])\s+|\n+", text)
    return [p.strip(" \t-•*") for p in parts if len(p.strip()) > 25]


def chunk_text(text: str, size: int = 1100) -> list:
    """Split long text into ~size-character pieces on paragraph/sentence boundaries."""
    text = re.sub(r"[ \t]+", " ", text.replace("\r", "")).strip()
    chunks, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        for piece in ([para] if len(para) <= size else sentences(para) or [para[:size]]):
            if len(cur) + len(piece) + 1 > size and cur:
                chunks.append(cur.strip()); cur = ""
            cur += piece + "\n"
    if cur.strip():
        chunks.append(cur.strip())
    return [c[:1500] for c in chunks]


def extract_text(filename: str, raw: bytes) -> str:
    """Read the text of an uploaded file (pdf, docx, pptx, txt)."""
    ext = Path(filename).suffix.lower()
    try:
        if ext == ".txt":
            return raw.decode("utf-8", errors="ignore")
        if ext == ".pdf":
            from pypdf import PdfReader
            return "\n\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(raw)).pages)
        if ext == ".docx":
            import docx
            d = docx.Document(io.BytesIO(raw))
            return "\n".join(p.text for p in d.paragraphs)
        if ext == ".pptx":
            from pptx import Presentation
            out = []
            for sl in Presentation(io.BytesIO(raw)).slides:
                for sh in sl.shapes:
                    if getattr(sh, "has_text_frame", False):
                        out.append(sh.text_frame.text)
            return "\n".join(out)
    except Exception as e:
        print(f"[TEXT] could not read {filename}: {type(e).__name__}: {e}")
    return ""


def save_knowledge(document_id: str, subject_id: str, title: str, text: str) -> int:
    chunks = chunk_text(text)[:300]
    db.add_many("Knowledge", [{"chunk_id": new_id("K"), "document_id": document_id,
                               "subject_id": subject_id, "title": title, "text": c} for c in chunks])
    return len(chunks)


def search(question: str, subject_ids: Optional[set]) -> list:
    """BM25-style ranking of knowledge chunks. Returns [(score, chunk), ...] best first."""
    q = set(tokens(question))
    rows = [r for r in db.rows("Knowledge") if not subject_ids or r["subject_id"] in subject_ids]
    if not q or not rows:
        return []
    docs = [Counter(tokens(r["title"] + " " + r["text"])) for r in rows]
    n, avg = len(rows), (sum(sum(d.values()) for d in docs) / len(rows)) or 1
    scored = []
    for r, d in zip(rows, docs):
        score, hit = 0.0, 0
        ln = sum(d.values()) or 1
        for t in q:
            f = d.get(t, 0)
            if not f:
                continue
            hit += 1
            df = sum(1 for x in docs if t in x)
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            score += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * ln / avg))
        if hit:
            score *= 1 + 0.5 * hit / len(q)          # reward chunks matching more of the question words
            if q & set(tokens(r["title"])):
                score *= 1.3
            scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    return scored


def best_sentences(question: str, chunks: list, limit: int) -> list:
    """Pick the sentences that match the question words, then show them in original order."""
    q = set(tokens(question))
    cand, rest = [], []
    for ci, ch in enumerate(chunks):
        for si, s in enumerate(sentences(ch["text"])):
            hit = len(q & set(tokens(s)))
            (cand if hit else rest).append((hit * 2 - ci * 0.4 - si * 0.05, ci, si, s))
    pool = cand or rest                      # no word match at all -> fall back to the opening lines
    top = sorted(pool, key=lambda x: -x[0])[:limit]
    return [t[3] for t in sorted(top, key=lambda x: (x[1], x[2]))]


def topic_of(question: str) -> str:
    t = " ".join(tokens(question)[:5])
    return t.title() if t else "Topic"


def make_mcqs(sents: list) -> str:
    pool = list({w for s in sents for w in tokens(s) if len(w) > 4})
    out, n = [], 0
    for s in sents:
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z\-]{4,}", s) if w.lower() not in STOP]
        if len(words) < 3 or len(pool) < 4:
            continue
        ans = max(words, key=len)
        wrong = random.Random(len(s)).sample([p for p in pool if p != ans.lower()], 3)
        opts = wrong + [ans.lower()]
        random.Random(len(ans)).shuffle(opts)
        letter = "ABCD"[opts.index(ans.lower())]
        n += 1
        blanked = re.sub(re.escape(ans), "_____", s, count=1)
        out.append(f"**Q{n}.** {blanked}\n" + "\n".join(f"  {'ABCD'[i]}) {o}" for i, o in enumerate(opts))
                   + f"\n  ✔ Answer: {letter}) {ans.lower()}")
        if n == 5:
            break
    return "\n\n".join(out)


def generate(question: str, subject_ids: Optional[set], mode: str) -> dict:
    ranked = search(question, subject_ids)
    if not ranked:
        return {"answer": "Is sawal ka jawab abhi mere data mein nahi mila. Admin se is topic ke notes upload "
                          "karwayein (Library ya Admin → Add knowledge), ya sawal ko alag shabdon mein poochhein." + FOOT_TXT}
    top = [c for _, c in ranked[:3]]
    topic = topic_of(question)
    if mode == "summary":
        body = "**Summary**\n" + " ".join(best_sentences(question, top, 3))
    elif mode == "notes":
        pts = best_sentences(question, top, 7)
        body = f"**Short notes: {topic}**\n" + "\n".join("• " + (p if len(p) < 170 else p[:167] + "…") for p in pts)
    elif mode == "exam":
        pts = best_sentences(question, top, 7)
        main = pts[1:-1] if len(pts) > 3 else pts
        body = (f"**Answer: {topic}**\n\n**Introduction:** {pts[0]}\n\n**Main points:**\n"
                + "\n".join(f"{i}. {p}" for i, p in enumerate(main[:5], 1))
                + (f"\n\n**Conclusion:** {pts[-1]}" if len(pts) > 3 else ""))
    elif mode == "mcq":
        sents = best_sentences(question, top, 8)
        body = "**MCQs**\n\n" + (make_mcqs(sents) or "Is topic par MCQ banane ke liye data kam hai.")
    else:
        body = " ".join(best_sentences(question, top[:2], 5))
    src = ", ".join(dict.fromkeys(c["title"] for c in top[:2] if c.get("title")))
    return {"answer": body + (f"\n\n📄 Source: {src}" if src else "") + FOOT_TXT}


# --------------------------------------------------------------------------
# Google Drive upload (through a small Apps Script, see drive_upload.gs)
# --------------------------------------------------------------------------
ALLOWED_EXT = {".pdf", ".docx", ".pptx", ".txt"}
MAX_B64 = 14_000_000   # ~10 MB file


def drive_upload(filename: str, data_b64: str) -> str:
    import httpx
    r = httpx.post(os.environ["DRIVE_UPLOAD_URL"], follow_redirects=True, timeout=120, json={
        "key": os.getenv("DRIVE_UPLOAD_KEY", ""), "name": filename, "data": data_b64,
        "mime": mimetypes.guess_type(filename)[0] or "application/octet-stream"})
    r.raise_for_status()
    out = r.json()
    if not out.get("url"):
        raise RuntimeError(out.get("error", "upload failed"))
    return out["url"]


def drive_file_id(link: str) -> str:
    m = re.search(r"(?:/d/|[?&]id=)([\w-]{20,})", link or "")
    return m.group(1) if m else ""


def drive_read(link: str):
    """Fetch a Drive file (through your Apps Script, so private files work too). -> (filename, bytes)"""
    import httpx
    fid = drive_file_id(link)
    if not fid:
        raise RuntimeError("not a Google Drive file link")
    r = httpx.post(os.environ["DRIVE_UPLOAD_URL"], follow_redirects=True, timeout=120,
                   json={"key": os.getenv("DRIVE_UPLOAD_KEY", ""), "action": "read", "id": fid})
    r.raise_for_status()
    out = r.json()
    if not out.get("data"):
        raise RuntimeError(out.get("error", "could not read file"))
    return out["name"], base64.b64decode(out["data"])


def index_document(doc: dict) -> int:
    """Read the document's Drive file and save its text for the AI. Returns number of chunks."""
    name, raw = drive_read(doc["drive_link"])
    text = extract_text(name, raw)
    if not text.strip():
        return 0
    db.delete_where("Knowledge", "document_id", doc["document_id"])
    return save_knowledge(doc["document_id"], doc["subject_id"], doc["pdf_name"], text)


seed()   # runs after every function above exists (seed uses save_knowledge)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
app = FastAPI(title="Sanvyara AI")


@app.exception_handler(Exception)
async def show_real_error(request, exc):
    """Show the real reason (e.g. Google Sheet refused the write) instead of a blank error."""
    print(f"[ERROR] {request.method} {request.url.path}: {type(exc).__name__}: {exc}")
    return JSONResponse(status_code=500, content={"detail": f"Save failed ({type(exc).__name__}): {str(exc)[:220]}"})


class Cred(BaseModel):
    email: str
    password: str
    name: str = ""


class NewCourse(BaseModel):
    course_name: str


class NewSemester(BaseModel):
    course_id: str
    semester_name: str


class NewSubject(BaseModel):
    semester_id: str
    subject_code: str = ""
    subject_name: str


class NewDoc(BaseModel):
    subject_id: str
    pdf_name: str
    drive_link: str
    document_type: str = "Notes"


class Upload(BaseModel):
    subject_id: str
    pdf_name: str
    filename: str
    data: str   # file content, base64
    document_type: str = "Notes"


class NewKnowledge(BaseModel):
    subject_id: str
    title: str
    text: str


class Ask(BaseModel):
    question: str
    semester_id: Optional[str] = None
    mode: str = "answer"


def session(user: dict) -> dict:
    return {"token": make_token(user["user_id"]), "user": public(user)}


def need(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise HTTPException(400, f"{label} is required")
    return value


@app.get("/")
def index():
    return FileResponse(BASE / "index.html")


@app.get("/api/info")
def info():
    return {"demo": db.mem is not None, "hint": db.hint, "ai": True,
            "drive": bool(os.getenv("DRIVE_UPLOAD_URL")), "footer": FOOTER}


# ---- auth ----
@app.post("/api/register")
def register(d: Cred):
    email, name = d.email.strip().lower(), need(d.name, "Name")
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "Enter a valid email")
    if len(d.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if find_user(email=email):
        raise HTTPException(400, "Email already registered")
    user = db.add("Users", {"user_id": new_id("U"), "name": name, "email": email,
                            "password_hash": hash_pw(d.password), "role": "student",
                            "status": "active", "created_at": now()})
    return session(user)


@app.post("/api/login")
def login(d: Cred):
    user = find_user(email=d.email.strip())
    if not user or not check_pw(d.password, user["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    if (user.get("status") or "active") != "active":
        raise HTTPException(403, "Account disabled")
    return session(user)


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return public(user)


# ---- course -> semester -> subject (one call returns the whole tree) ----
@app.get("/api/tree")
def tree(user: dict = Depends(current_user)):
    sems, subs = db.rows("Semesters"), db.rows("Subjects")
    return [{**c, "semesters": [
                {**s, "subjects": [x for x in subs if x["semester_id"] == s["semester_id"]]}
                for s in sems if s["course_id"] == c["course_id"]]}
            for c in db.rows("Courses")]


@app.post("/api/admin/course")
def add_course(d: NewCourse, admin: dict = Depends(admin_only)):
    return db.add("Courses", {"course_id": new_id("C"), "course_name": need(d.course_name, "Course name")})


@app.post("/api/admin/semester")
def add_semester(d: NewSemester, admin: dict = Depends(admin_only)):
    if not any(c["course_id"] == d.course_id for c in db.rows("Courses")):
        raise HTTPException(400, "Course not found")
    return db.add("Semesters", {"semester_id": new_id("SEM"), "course_id": d.course_id,
                                "semester_name": need(d.semester_name, "Semester name")})


@app.post("/api/admin/subject")
def add_subject(d: NewSubject, admin: dict = Depends(admin_only)):
    if not any(s["semester_id"] == d.semester_id for s in db.rows("Semesters")):
        raise HTTPException(400, "Semester not found")
    return db.add("Subjects", {"subject_id": new_id("SUB"), "semester_id": d.semester_id,
                               "subject_code": d.subject_code.strip(),
                               "subject_name": need(d.subject_name, "Subject name")})


# ---- knowledge: your own text that the AI answers from ----
@app.post("/api/admin/knowledge")
def add_knowledge(d: NewKnowledge, admin: dict = Depends(admin_only)):
    if not any(s["subject_id"] == d.subject_id for s in db.rows("Subjects")):
        raise HTTPException(400, "Subject not found")
    title, text = need(d.title, "Title"), need(d.text, "Text")
    if len(text) > 200_000:
        raise HTTPException(400, "Text is too long (max 200,000 characters)")
    doc = db.add("Documents", {"document_id": new_id("D"), "subject_id": d.subject_id,
                               "pdf_name": title, "drive_link": "", "document_type": "Notes",
                               "uploaded_by": admin["user_id"], "uploaded_at": now()})
    return {"chunks": save_knowledge(doc["document_id"], d.subject_id, title, text)}


# ---- documents (Google Drive links) ----
@app.get("/api/documents")
def documents(user: dict = Depends(current_user)):
    return db.rows("Documents")


@app.post("/api/documents")
def add_document(d: NewDoc, user: dict = Depends(current_user)):
    if not any(s["subject_id"] == d.subject_id for s in db.rows("Subjects")):
        raise HTTPException(400, "Subject not found")
    link = d.drive_link.strip()
    if not re.match(r"^https?://", link):
        raise HTTPException(400, "Link must start with http:// or https://")
    doc = dict(db.add("Documents", {"document_id": new_id("D"), "subject_id": d.subject_id,
                                    "pdf_name": need(d.pdf_name, "Document name"), "drive_link": link,
                                    "document_type": d.document_type.strip() or "Notes",
                                    "uploaded_by": user["user_id"], "uploaded_at": now()}))
    doc["chunks"] = 0
    if os.getenv("DRIVE_UPLOAD_URL") and drive_file_id(link):
        try:
            doc["chunks"] = index_document(doc)
        except Exception as e:
            print(f"[DRIVE] could not read {link}: {type(e).__name__}: {e}")
    return doc


@app.post("/api/admin/sync")
def sync_drive(admin: dict = Depends(admin_only)):
    """Read every Drive document that the AI has not learned from yet."""
    if not os.getenv("DRIVE_UPLOAD_URL"):
        raise HTTPException(400, "Google Drive is not connected on the server yet (see README step 2)")
    learned = {k["document_id"] for k in db.rows("Knowledge")}
    done = failed = 0
    for doc in db.rows("Documents"):
        if doc["document_id"] in learned or not drive_file_id(doc.get("drive_link", "")):
            continue
        try:
            if index_document(doc):
                done += 1
            else:
                failed += 1          # file opened but has no readable text
        except Exception as e:
            failed += 1
            print(f"[SYNC] {doc.get('pdf_name')}: {type(e).__name__}: {e}")
    return {"learned": done, "failed": failed, "documents_known": len(learned)}


@app.post("/api/upload")
def upload(d: Upload, user: dict = Depends(current_user)):
    if not any(s["subject_id"] == d.subject_id for s in db.rows("Subjects")):
        raise HTTPException(400, "Subject not found")
    filename = Path(d.filename).name.strip()
    if Path(filename).suffix.lower() not in ALLOWED_EXT:
        raise HTTPException(400, "Allowed files: PDF, Word, PowerPoint, Excel, TXT")
    if len(d.data) > MAX_B64:
        raise HTTPException(400, "File is too large (max 10 MB)")
    if not os.getenv("DRIVE_UPLOAD_URL"):
        raise HTTPException(400, "Google Drive is not connected on the server yet (see README step 2)")
    title = need(d.pdf_name, "Document name")
    try:
        raw = base64.b64decode(d.data, validate=True)
    except Exception:
        raise HTTPException(400, "File could not be read")
    try:
        link = drive_upload(filename, d.data)
    except Exception as e:
        print(f"[DRIVE] upload failed: {type(e).__name__}: {e}")
        raise HTTPException(502, "Could not save the file to Google Drive")
    text = extract_text(filename, raw)
    doc = db.add("Documents", {"document_id": new_id("D"), "subject_id": d.subject_id,
                               "pdf_name": title, "drive_link": link,
                               "document_type": d.document_type.strip() or "Notes",
                               "uploaded_by": user["user_id"], "uploaded_at": now()})
    doc = dict(doc)
    doc["chunks"] = save_knowledge(doc["document_id"], d.subject_id, title, text) if text.strip() else 0
    return doc   # chunks == 0 means the file has no readable text (scanned PDF?)


@app.delete("/api/documents/{document_id}")
def delete_document(document_id: str, user: dict = Depends(current_user)):
    doc = next((x for x in db.rows("Documents") if x["document_id"] == document_id), None)
    if not doc:
        raise HTTPException(404, "Document not found")
    if user["role"] != "admin" and doc.get("uploaded_by") != user["user_id"]:
        raise HTTPException(403, "Not your document")
    db.delete("Documents", "document_id", document_id)
    db.delete_where("Knowledge", "document_id", document_id)
    return {"ok": True}


# ---- AI chat ----
@app.post("/api/ask")
def ask(d: Ask, user: dict = Depends(current_user)):
    q = need(d.question, "Question")
    if len(q) > 1000:
        raise HTTPException(400, "Question is too long (max 1000 characters)")
    if d.mode not in MODES:
        raise HTTPException(400, "Unknown mode")
    ids = None
    if d.semester_id:
        ids = {x["subject_id"] for x in db.rows("Subjects") if x["semester_id"] == d.semester_id}
    result = generate(q, ids, d.mode)
    db.add("Chats", {"message_id": new_id("M"), "user_id": user["user_id"], "mode": d.mode,
                     "question": q, "answer": result["answer"], "created_at": now()})
    return result


@app.get("/api/history")
def history(user: dict = Depends(current_user)):
    mine = [c for c in db.rows("Chats") if c.get("user_id") == user["user_id"]][-30:]
    return [{"question": c["question"], "answer": c["answer"]} for c in reversed(mine)]   # newest first
