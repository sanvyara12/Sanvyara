"""
Sanvyara AI - single-file backend.

  * Serves index.html (the whole frontend)
  * JSON API under /api/*
  * Database = Google Sheet (one tab per table). If no Sheet is configured
    it falls back to in-memory demo data so you can try it instantly.

Run:  uvicorn server:app --reload --port 8000
"""
import base64
import hashlib
import hmac
import json
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
from fastapi.responses import FileResponse
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
    "Chats": ["message_id", "user_id", "subject_id", "mode", "question", "answer",
              "sources", "created_at"],
}


def new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:8].upper()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sheet_id() -> str:
    raw = os.getenv("GOOGLE_SHEET_ID") or os.getenv("GOOGLE_SHEET_URL") or ""
    m = re.search(r"/spreadsheets/d/([\w-]+)", raw)
    return m.group(1) if m else raw.strip()


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}   # tab -> (time, rows)   (avoids hitting Google's read quota)
        self.ws = {}      # tab -> worksheet
        self.cols = {}    # tab -> header row
        self.mem = None   # dict of lists when running without a Sheet

        info = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        keyfile = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
        try:
            if not sheet_id() or not (info or keyfile):
                raise RuntimeError("GOOGLE_SHEET_URL / service account not set")
            import gspread
            gc = gspread.service_account_from_dict(json.loads(info)) if info \
                else gspread.service_account(filename=keyfile)
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
        except Exception as e:
            print(f"[DB] Google Sheet NOT connected ({e}). Using in-memory demo data - resets on restart.")
            self.mem = {t: [] for t in TABS}

    def rows(self, tab: str) -> list:
        with self.lock:
            if self.mem is not None:
                return [dict(r) for r in self.mem[tab]]
            hit = self.cache.get(tab)
            if hit and time.time() - hit[0] < 10:
                return hit[1]
            data = self.ws[tab].get_all_records(numericise_ignore=["all"])
            self.cache[tab] = (time.time(), data)
            return data

    def add(self, tab: str, row: dict) -> dict:
        with self.lock:
            if self.mem is not None:
                self.mem[tab].append(dict(row))
            else:
                # RAW => text starting with "=" is stored as text, never as a formula
                self.ws[tab].append_row([str(row.get(c, "")) for c in self.cols[tab]],
                                        value_input_option="RAW")
                self.cache.pop(tab, None)
        return row

    def delete(self, tab: str, col: str, val: str) -> bool:
        with self.lock:
            if self.mem is not None:
                before = len(self.mem[tab])
                self.mem[tab] = [r for r in self.mem[tab] if str(r.get(col)) != val]
                return len(self.mem[tab]) < before
            data = self.ws[tab].get_all_records(numericise_ignore=["all"])
            for i, r in enumerate(data, start=2):   # row 1 = headers
                if str(r.get(col)) == val:
                    self.ws[tab].delete_rows(i)
                    self.cache.pop(tab, None)
                    return True
        return False


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


seed()

# --------------------------------------------------------------------------
# AI answers (demo keyword search; uses OpenAI if OPENAI_API_KEY is set)
# --------------------------------------------------------------------------
DEMO_CHUNKS = {
    "dbms": [
        {"pdf_name": "DBMS Unit 1 Notes", "page": 12, "text": "Normalization is the process of organizing data in a database to reduce redundancy and improve data integrity. The main normal forms are 1NF, 2NF, 3NF and BCNF."},
        {"pdf_name": "DBMS Unit 1 Notes", "page": 15, "text": "1NF (First Normal Form): A relation is in 1NF if it contains only atomic (indivisible) values and each column contains values of a single type."},
        {"pdf_name": "Normalization & Transactions", "page": 8, "text": "2NF: A relation is in 2NF if it is in 1NF and every non-prime attribute is fully functionally dependent on the primary key (no partial dependency)."},
        {"pdf_name": "Normalization & Transactions", "page": 14, "text": "3NF: A relation is in 3NF if it is in 2NF and no non-prime attribute is transitively dependent on the primary key."},
        {"pdf_name": "DBMS Unit 1 Notes", "page": 22, "text": "ACID properties of transactions: Atomicity, Consistency, Isolation, Durability. These ensure reliable processing of database transactions."},
    ],
    "operating systems": [
        {"pdf_name": "OS Notes", "page": 5, "text": "An operating system is system software that manages computer hardware and software resources and provides common services for computer programs."},
    ],
}

MODES = {
    "answer": "{q}",
    "summary": "Give a clear short summary of: {q}",
    "notes": "Create concise revision short notes in bullet points on: {q}",
    "exam": "Write a 5-mark exam style answer on: {q}",
    "mcq": "Create 5 multiple choice questions (with correct answers and a one-line explanation) on: {q}",
}

SYSTEM = ("You are Sanvyara AI, a helpful college learning assistant. Answer ONLY using the provided "
          "document context. If the context is not enough, say so clearly. Cite document name and page. "
          "Keep it clear and student-friendly.")


def retrieve(question: str, subject_name: str, top_k: int = 4) -> list:
    pool = [c for key, chunks in DEMO_CHUNKS.items()
            if not subject_name or key in subject_name.lower() for c in chunks]
    words = {w for w in re.findall(r"[a-z0-9]+", question.lower())
             if len(w) > 3 or any(ch.isdigit() for ch in w)}   # skip tiny words like "is", "the"
    scored = [(sum(w in c["text"].lower() for w in words), c) for c in pool]
    return [c for s, c in sorted(scored, key=lambda x: -x[0]) if s > 0][:top_k]


def ask_openai(task: str, context: str) -> str:
    import httpx
    r = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
        json={"model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"), "temperature": 0.3, "max_tokens": 800,
              "messages": [{"role": "system", "content": SYSTEM},
                           {"role": "user", "content": f"Context from college PDFs:\n{context}\n\nTask: {task}"}]},
        timeout=45,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def generate(question: str, subject_name: str, mode: str) -> dict:
    chunks = retrieve(question, subject_name)
    if not chunks:
        return {"answer": "I could not find this in the indexed documents. Try rephrasing or pick another subject.",
                "sources": []}
    sources = [{"pdf_name": c["pdf_name"], "page": c["page"]} for c in chunks]

    if os.getenv("OPENAI_API_KEY"):
        try:
            context = "\n\n".join(f"[{c['pdf_name']}, page {c['page']}]\n{c['text']}" for c in chunks)
            return {"answer": ask_openai(MODES[mode].format(q=question), context), "sources": sources}
        except Exception as e:
            print(f"[AI] OpenAI error: {e}")

    if mode == "mcq":
        return {"answer": "MCQ generation needs OPENAI_API_KEY to be set on the server.", "sources": sources}
    lines = ["Answer (from indexed documents):", ""]
    for i, c in enumerate(chunks, 1):
        lines += [f"{i}. {c['text']}", f"   - Source: {c['pdf_name']}, page {c['page']}", ""]
    lines.append("(Demo answer - set OPENAI_API_KEY for a written answer.)")
    return {"answer": "\n".join(lines), "sources": sources}


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
app = FastAPI(title="Sanvyara AI")


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


class Ask(BaseModel):
    question: str
    subject_id: Optional[str] = None
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
    return {"demo": db.mem is not None, "ai": bool(os.getenv("OPENAI_API_KEY"))}


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
    return db.add("Documents", {"document_id": new_id("D"), "subject_id": d.subject_id,
                                "pdf_name": need(d.pdf_name, "Document name"), "drive_link": link,
                                "document_type": d.document_type.strip() or "Notes",
                                "uploaded_by": user["user_id"], "uploaded_at": now()})


@app.delete("/api/documents/{document_id}")
def delete_document(document_id: str, user: dict = Depends(current_user)):
    doc = next((x for x in db.rows("Documents") if x["document_id"] == document_id), None)
    if not doc:
        raise HTTPException(404, "Document not found")
    if user["role"] != "admin" and doc.get("uploaded_by") != user["user_id"]:
        raise HTTPException(403, "Not your document")
    db.delete("Documents", "document_id", document_id)
    return {"ok": True}


# ---- AI chat ----
@app.post("/api/ask")
def ask(d: Ask, user: dict = Depends(current_user)):
    q = need(d.question, "Question")
    if len(q) > 1000:
        raise HTTPException(400, "Question is too long (max 1000 characters)")
    if d.mode not in MODES:
        raise HTTPException(400, "Unknown mode")
    subject = next((s for s in db.rows("Subjects") if s["subject_id"] == d.subject_id), None)
    result = generate(q, subject["subject_name"] if subject else "", d.mode)
    db.add("Chats", {"message_id": new_id("M"), "user_id": user["user_id"],
                     "subject_id": d.subject_id or "", "mode": d.mode, "question": q,
                     "answer": result["answer"], "sources": json.dumps(result["sources"]),
                     "created_at": now()})
    return result


@app.get("/api/history")
def history(user: dict = Depends(current_user)):
    mine = [c for c in db.rows("Chats") if c.get("user_id") == user["user_id"]][-30:]
    out = []
    for c in reversed(mine):   # newest first
        try:
            sources = json.loads(c.get("sources") or "[]")
        except ValueError:
            sources = []
        out.append({"question": c["question"], "answer": c["answer"], "sources": sources})
    return out
