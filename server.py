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
import queue
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import bcrypt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
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
    "Settings": ["key", "value"],
    "UserData": ["item_id", "user_id", "kind", "ref", "data", "created_at"],  # profile, bookmarks, quiz results ...
    "DocMeta": ["document_id", "unit", "pages"],
}
CACHE_SECONDS = int(os.getenv("CACHE_SECONDS", "300"))   # after this, data is refreshed in the background


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
        self.locks = {t: threading.RLock() for t in TABS}   # one lock per tab, so tabs don't block each other
        self.cache = {}   # tab -> (time, rows)   (reads are served from memory)
        self.refreshing = set()
        self.http = None  # reused HTTPS connection to Apps Script
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
        if self.http is None:
            self.http = httpx.Client(follow_redirects=True, timeout=60)
        try:
            r = self.http.post(self.script, json={"key": os.getenv("DRIVE_UPLOAD_KEY", ""), **payload})
            r.raise_for_status()
            out = r.json()
        except ValueError:
            raise ScriptDBError("Apps Script returned a web page, not data. In Apps Script: run authorize() once and click Allow, "
                                "then Deploy -> Manage deployments -> Edit -> New version, access = Anyone. "
                                "Test: open the /exec URL in a browser, it must show {\"ok\":true}")
        except Exception as e:
            raise ScriptDBError("Apps Script not reachable - check DRIVE_UPLOAD_URL is the /exec link "
                                f"[{type(e).__name__}]")
        if out.get("error"):
            raise ScriptDBError("Apps Script says: " + str(out["error"]) +
                                " (re-deploy drive_upload.gs as a NEW VERSION and set SHEET_ID in it)")
        return out

    def _connect_script(self, url):
        self.script = url
        for attempt in range(3):          # a slow first reply must not push the app into demo mode
            try:
                self._call(action="init", tabs=TABS)
                break
            except ScriptDBError:
                if attempt == 2:
                    raise
                time.sleep(2)
        print("[DB] Connected to Google Sheet through Apps Script")

    def _fetch(self, tab: str) -> list:
        if self.script:
            return self._call(action="rows", tab=tab, cols=TABS[tab])["rows"]
        return self.ws[tab].get_all_records(numericise_ignore=["all"])

    def _refresh(self, tab: str) -> None:
        try:
            with self.locks[tab]:
                self.cache[tab] = (time.time(), self._fetch(tab))
        except Exception as e:
            print(f"[DB] refresh {tab} failed: {type(e).__name__}: {e}")
        finally:
            self.refreshing.discard(tab)

    def rows(self, tab: str) -> list:
        """Answer from memory at once; if the copy is old, refresh it in the background."""
        if self.mem is not None:
            return [dict(r) for r in self.mem[tab]]
        hit = self.cache.get(tab)
        if hit:
            if time.time() - hit[0] > CACHE_SECONDS and tab not in self.refreshing:
                self.refreshing.add(tab)
                threading.Thread(target=self._refresh, args=(tab,), daemon=True).start()
            return hit[1]
        with self.locks[tab]:
            if tab not in self.cache:
                self.cache[tab] = (time.time(), self._fetch(tab))
            return self.cache[tab][1]

    def preload(self) -> None:
        """Load every tab once at start-up (one request with the new drive_upload.gs)."""
        if self.mem is not None:
            return
        try:
            if self.script:
                try:
                    for t in TABS:             # hold writes while downloading, so nothing gets lost
                        self.locks[t].acquire()
                    try:
                        data = self._call(action="all", tabs=TABS)["tabs"]
                        for t in TABS:
                            self.cache.setdefault(t, (time.time(), data[t]))
                    finally:
                        for t in TABS:
                            self.locks[t].release()
                    return
                except ScriptDBError:
                    pass                       # old script version: load tab by tab
            for t in TABS:
                self.rows(t)
        except Exception as e:
            print(f"[DB] preload failed: {type(e).__name__}: {e}")

    def reload(self) -> None:
        """Forget the memory copy (after editing the Sheet by hand)."""
        for t in TABS:
            with self.locks[t]:
                self.cache.pop(t, None)
        self.preload()

    def add(self, tab: str, row: dict) -> dict:
        self.add_many(tab, [row])
        return row

    def add_many(self, tab: str, rows: list) -> None:
        if not rows:
            return
        with self.locks[tab]:
            if self.mem is not None:
                self.mem[tab].extend(dict(r) for r in rows)
                return
            clean = [{c: str(r.get(c, "")) for c in TABS[tab]} for r in rows]
            if self.script:
                self._call(action="append", tab=tab, cols=TABS[tab], rows=clean)
            else:
                # RAW => text starting with "=" is stored as text, never as a formula
                self.ws[tab].append_rows([[str(r.get(c, "")) for c in self.cols[tab]] for r in rows],
                                         value_input_option="RAW")
            hit = self.cache.get(tab)
            if hit:                            # update memory copy, no need to download again
                self.cache[tab] = (hit[0], hit[1] + clean)

    def delete_where(self, tab: str, col: str, val: str) -> None:
        """Delete every row where col == val (e.g. all text chunks of one document)."""
        with self.locks[tab]:
            if self.mem is not None:
                self.mem[tab] = [r for r in self.mem[tab] if str(r.get(col)) != val]
                return
            if self.script:
                self._call(action="delete", tab=tab, cols=TABS[tab], col=col, val=val)
            else:
                data = self.ws[tab].get_all_records(numericise_ignore=["all"])
                idx = [i for i, r in enumerate(data, start=2) if str(r.get(col)) == val]
                runs = []                        # group into contiguous runs (fewer Google requests)
                for i in idx:
                    if runs and i == runs[-1][1] + 1:
                        runs[-1][1] = i
                    else:
                        runs.append([i, i])
                for a, b in reversed(runs):      # bottom first so row numbers stay valid
                    self.ws[tab].delete_rows(a, b)
            hit = self.cache.get(tab)
            if hit:
                self.cache[tab] = (hit[0], [r for r in hit[1] if str(r.get(col)) != val])


db = Store()


def latest(kind: str, ref: str) -> Optional[dict]:
    return next((r for r in reversed(db.rows("UserData")) if r["kind"] == kind and r["ref"] == ref), None)


def put_item(user_id: str, kind: str, ref: str, data) -> dict:
    return db.add("UserData", {"item_id": new_id("I"), "user_id": user_id, "kind": kind, "ref": ref,
                               "data": json.dumps(data, ensure_ascii=False), "created_at": now()})


def item_data(row: Optional[dict], default=None):
    try:
        return json.loads(row["data"]) if row else default
    except ValueError:
        return default


def is_disabled(user: dict) -> bool:
    return (user.get("status") or "active") != "active" or bool(item_data(latest("disabled", user["user_id"])))


def password_of(user: dict) -> str:
    return item_data(latest("pw", user["user_id"])) or user["password_hash"]

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
    if not user or is_disabled(user):
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
MODES = {"answer", "summary", "notes", "exam", "mcq", "flashcards", "questions"}

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


def extract_pages(filename: str, raw: bytes) -> list:
    """[(page_number, text)] - PDF pages / PPT slides keep their number, other files use 0."""
    ext = Path(filename).suffix.lower()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            return [(i, p.extract_text() or "") for i, p in enumerate(PdfReader(io.BytesIO(raw)).pages, 1)]
        if ext == ".pptx":
            from pptx import Presentation
            return [(i, "\n".join(sh.text_frame.text for sh in sl.shapes if getattr(sh, "has_text_frame", False)))
                    for i, sl in enumerate(Presentation(io.BytesIO(raw)).slides, 1)]
    except Exception as e:
        print(f"[TEXT] could not read {filename}: {type(e).__name__}: {e}")
        return []
    return [(0, extract_text(filename, raw))]


def save_knowledge(document_id: str, subject_id: str, title: str, text) -> int:
    pages = [(0, text)] if isinstance(text, str) else text
    rows = [{"chunk_id": new_id("K") + (f"-p{n}" if n else ""), "document_id": document_id,
             "subject_id": subject_id, "title": title, "text": c}
            for n, t in pages if t.strip() for c in chunk_text(t)][:1000]
    db.add_many("Knowledge", rows)
    return len(rows)


def page_of(chunk: dict) -> int:
    m = re.search(r"-p(\d+)$", str(chunk.get("chunk_id", "")))
    return int(m.group(1)) if m else 0


def save_meta(document_id: str, unit: str, pages: int) -> None:
    db.delete_where("DocMeta", "document_id", document_id)
    db.add("DocMeta", {"document_id": document_id, "unit": unit, "pages": pages or ""})


def meta_map() -> dict:
    return {m["document_id"]: m for m in db.rows("DocMeta")}


TOKEN_CACHE = {}   # chunk -> word counts, so notes are not re-processed on every question


def chunk_words(r: dict) -> Counter:
    key = (r.get("chunk_id"), r["title"], r["text"])
    if key not in TOKEN_CACHE:
        TOKEN_CACHE[key] = Counter(tokens(r["title"] + " " + r["text"]))
    return TOKEN_CACHE[key]


def search(question: str, rows: list) -> list:
    """BM25-style ranking of knowledge chunks. Returns [(score, chunk), ...] best first."""
    q = set(tokens(question))
    if not q or not rows:
        return []
    docs = [chunk_words(r) for r in rows]
    n, avg = len(rows), (sum(sum(d.values()) for d in docs) / len(rows)) or 1
    dfs = {t: sum(1 for x in docs if t in x) for t in q}
    scored = []
    for r, d in zip(rows, docs):
        score, hit = 0.0, 0
        ln = sum(d.values()) or 1
        for t in q:
            f = d.get(t, 0)
            if not f:
                continue
            hit += 1
            df = dfs[t]
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
    cand, rest, seen = [], [], set()
    for ci, ch in enumerate(chunks):
        for si, s in enumerate(sentences(ch["text"])):
            if s.lower() in seen:            # the same line in two documents is shown once
                continue
            seen.add(s.lower())
            hit = len(q & set(tokens(s)))
            (cand if hit else rest).append((hit * 2 - ci * 0.4 - si * 0.05, ci, si, s))
    pool = cand or rest                      # no word match at all -> fall back to the opening lines
    top = sorted(pool, key=lambda x: -x[0])[:limit]
    return [t[3] for t in sorted(top, key=lambda x: (x[1], x[2]))]


def topic_of(question: str) -> str:
    t = " ".join(tokens(question)[:5])
    return t.title() if t else "Topic"


DEF_RE = re.compile(r"^(?:An? |The )?([A-Z0-9][\w()\-/ ]{1,60}?)\s+(?:is|are|means|refers to|stands for)\s+(.{15,})$")


def definitions(sents: list) -> list:
    out, seen = [], set()
    for s in sents:
        m = DEF_RE.match(s)
        if m and len(m.group(1).split()) <= 6 and m.group(1).lower() not in seen | {"it", "this", "that", "these", "there"}:
            seen.add(m.group(1).lower())
            out.append((m.group(1).strip(), s))
    return out


def quiz_items(sents: list, count: int, difficulty: str, seed=None) -> list:
    """Fill-in-the-blank questions from the notes. Easy = long clear word, hard = a shorter, less obvious one."""
    rnd = random.Random(seed)
    pool = sorted({w.lower() for s in sents for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", s) if w.lower() not in STOP})
    out, sents = [], list(dict.fromkeys(sents))
    rnd.shuffle(sents)
    for s in sents:
        words = sorted({w for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", s) if w.lower() not in STOP},
                       key=lambda w: (-len(w), w))
        if len(words) < 3 or len(pool) < 4:
            continue
        ans = words[0] if difficulty == "easy" else words[-1] if difficulty == "hard" else words[len(words) // 2]
        others = [p for p in pool if p != ans.lower()]
        if difficulty == "hard":                       # wrong options of similar length are harder to rule out
            others = sorted(others, key=lambda p: abs(len(p) - len(ans)))[:8]
        opts = rnd.sample(others, 3) + [ans.lower()]
        rnd.shuffle(opts)
        out.append({"question": re.sub(r"\b" + re.escape(ans) + r"\b", "_____", s), "options": opts,
                    "answer": opts.index(ans.lower()), "explanation": s})
        if len(out) == count:
            break
    return out


def knowledge_rows(subject_ids: Optional[set], document_id: str = "", unit: str = "") -> list:
    rows = db.rows("Knowledge")
    if subject_ids is not None:
        rows = [r for r in rows if r["subject_id"] in subject_ids]
    if document_id:
        rows = [r for r in rows if r["document_id"] == document_id]
    if unit:
        ids = {k for k, m in meta_map().items() if str(m["unit"]) == unit}
        rows = [r for r in rows if r["document_id"] in ids]
    return rows


def generate(question: str, rows: list, mode: str, marks: int = 5, whole: bool = False) -> dict:
    ranked = search(question, rows)
    top = [c for _, c in ranked[:3]]
    if whole:                          # a tool run on one PDF / unit: use all of it
        top = rows[:40]
    if not top:
        return {"answer": "Is sawal ka jawab abhi mere data mein nahi mila. Admin se is topic ke notes upload "
                          "karwayein, ya sawal ko alag shabdon mein poochhein." + FOOT_TXT, "sources": []}
    topic = topic_of(question) if tokens(question) else str(top[0].get("title") or "Topic")
    pick = lambda n: best_sentences(question, top, n)
    bullets = lambda xs: "\n".join("• " + x for x in xs)
    if mode == "summary":
        body = f"**Summary: {topic}**\n" + " ".join(pick(8 if whole else 4))
    elif mode == "notes":
        body = f"**Short notes: {topic}**\n" + bullets(pick(12 if whole else 7))
    elif mode == "exam":
        pts = pick({2: 3, 5: 7, 10: 14}.get(marks, 7))
        mid = pts[1:-1] if len(pts) > 3 else pts[1:]
        body = f"**{marks} marks answer: {topic}**\n\n**Introduction:** {pts[0]}" if pts else f"**{topic}**"
        if mid:
            body += "\n\n**Main points:**\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(mid, 1))
        if len(pts) > 3:
            body += f"\n\n**Conclusion:** {pts[-1]}"
    elif mode == "mcq":
        qs = quiz_items(pick(14), 5, "medium", seed=len(question))
        body = "**MCQs**\n\n" + ("\n\n".join(
            f"**Q{i}.** {q['question']}\n" + "\n".join(f"  {'ABCD'[j]}) {o}" for j, o in enumerate(q["options"]))
            + f"\n  ✔ Answer: {'ABCD'[q['answer']]}" for i, q in enumerate(qs, 1))
            or "Is topic par MCQ banane ke liye data kam hai.")
    elif mode in ("flashcards", "questions"):
        defs = definitions([x for c in top for x in sentences(c["text"])])[:10]
        if mode == "flashcards":
            body = "**Flashcards**\n\n" + ("\n\n".join(f"**{t}**\n{d}" for t, d in defs) or "")
        else:
            body = "**Important questions**\n\n" + "\n".join(
                f"{i}. What is {t}? Explain with an example." for i, (t, _) in enumerate(defs, 1))
        if not defs:
            body += "Is data mein clear definitions nahi mili."
    else:
        sents = best_sentences(question, top[:2], 8)
        ex = [x for x in sents if re.search(r"\b(example|e\.g\.|such as|for instance|jaise)", x, re.I)]
        rest = [x for x in sents if x not in ex]
        body = f"**{topic}**\n\n**Explanation**\n" + " ".join(rest[:3] or sents[:3])
        if ex:
            body += "\n\n**Examples**\n" + bullets(ex)
        if rest[3:]:
            body += "\n\n**Important points**\n" + bullets(rest[3:])
    sources, seen = [], set()
    for c in top[:3]:
        key = (c["document_id"], page_of(c))
        if key not in seen:
            seen.add(key)
            sources.append({"document_id": c["document_id"], "title": c["title"], "page": page_of(c),
                            "text": c["text"][:400]})
    return {"answer": body + FOOT_TXT, "sources": sources}


# --------------------------------------------------------------------------
# Google Drive upload (through a small Apps Script, see drive_upload.gs)
# --------------------------------------------------------------------------
ALLOWED_EXT = {".pdf", ".docx", ".pptx", ".txt"}
MAX_B64 = 28_000_000   # ~20 MB file


def setting(key: str) -> str:
    return next((str(r["value"]) for r in reversed(db.rows("Settings")) if r["key"] == key), "")


def folder_id_from(link: str) -> str:
    """Folder ID from a Drive folder link (or a bare ID)."""
    link = (link or "").strip()
    m = re.search(r"/folders/([\w-]{15,})", link) or re.search(r"drive\.google\.com/.*[?&]id=([\w-]{15,})", link)
    if m:
        return m.group(1)
    return link if re.fullmatch(r"[\w-]{15,}", link) else ""


def script_post(payload: dict, timeout: int = 120) -> dict:
    import httpx
    r = httpx.post(os.environ["DRIVE_UPLOAD_URL"], follow_redirects=True, timeout=timeout,
                   json={"key": os.getenv("DRIVE_UPLOAD_KEY", ""), **payload})
    r.raise_for_status()
    return r.json()


def drive_upload(filename: str, data_b64: str) -> str:
    folder = setting("drive_folder_id")
    out = script_post({"name": filename, "data": data_b64, "folderId": folder,
                       "mime": mimetypes.guess_type(filename)[0] or "application/octet-stream"})
    if not out.get("url"):
        raise RuntimeError(out.get("error", "upload failed"))
    if folder and out.get("folderId") != folder:
        raise RuntimeError("old Apps Script is deployed - deploy the new drive_upload.gs as a NEW VERSION")
    return out["url"]


def drive_file_id(link: str) -> str:
    m = re.search(r"(?:/d/|[?&]id=)([\w-]{20,})", link or "")
    return m.group(1) if m else ""


def drive_read(link: str):
    """Fetch a Drive file (through your Apps Script, so private files work too). -> (filename, bytes)"""
    fid = drive_file_id(link)
    if not fid:
        raise RuntimeError("not a Google Drive file link")
    out = script_post({"action": "read", "id": fid})
    if not out.get("data"):
        raise RuntimeError(out.get("error", "could not read file"))
    return out["name"], base64.b64decode(out["data"])


def index_document(doc: dict) -> int:
    """Read the document's Drive file and save its text for the AI. Returns number of chunks."""
    name, raw = drive_read(doc["drive_link"])
    pages = extract_pages(name, raw)
    if not any(t.strip() for _, t in pages):
        return 0
    db.delete_where("Knowledge", "document_id", doc["document_id"])
    save_meta(doc["document_id"], meta_map().get(doc["document_id"], {}).get("unit", ""),
              pages[-1][0])
    return save_knowledge(doc["document_id"], doc["subject_id"], doc["pdf_name"], pages)


# Chat history is written in the background so the student gets the answer immediately.
chat_queue = queue.Queue()


def chat_writer():
    while True:
        row, meta = chat_queue.get()
        try:
            db.add("Chats", row)
            put_item(row["user_id"], "msg", row["message_id"], meta)
        except Exception as e:
            print(f"[CHAT] history not saved: {type(e).__name__}: {e}")
        finally:
            chat_queue.task_done()


threading.Thread(target=chat_writer, daemon=True).start()

seed()   # runs after every function above exists (seed uses save_knowledge)
threading.Thread(target=db.preload, daemon=True).start()


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
app = FastAPI(title="Sanvyara AI")
app.add_middleware(GZipMiddleware, minimum_size=1000)


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
    unit: str = ""


class Upload(BaseModel):
    subject_id: str
    pdf_name: str
    filename: str
    data: str   # file content, base64
    document_type: str = "Notes"
    unit: str = ""


class NewKnowledge(BaseModel):
    subject_id: str
    title: str
    text: str


class Folder(BaseModel):
    link: str


class Ask(BaseModel):
    question: str
    semester_id: Optional[str] = None
    mode: str = "answer"
    subject_id: str = ""
    document_id: str = ""
    unit: str = ""
    marks: int = 5
    whole: bool = False
    conversation_id: str = ""


class Item(BaseModel):
    kind: str
    ref: str = ""
    data: dict = {}


class Quiz(BaseModel):
    subject_id: str
    unit: str = ""
    count: int = 5
    difficulty: str = "medium"


class Password(BaseModel):
    old: str = ""
    new: str


class Rename(BaseModel):
    name: str
    code: str = ""


class Status(BaseModel):
    disabled: bool


class DocEdit(BaseModel):
    pdf_name: str
    subject_id: str
    drive_link: str = ""
    document_type: str = "Notes"
    unit: str = ""


class Prefs(BaseModel):
    categories: str
    allow_download: bool = True


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
    if not user or not check_pw(d.password, password_of(user)):
        raise HTTPException(401, "Invalid email or password")
    if is_disabled(user):
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
    meta = meta_map()
    return [{**d, "unit": meta.get(d["document_id"], {}).get("unit", ""),
             "pages": meta.get(d["document_id"], {}).get("pages", "")} for d in db.rows("Documents")]


@app.post("/api/documents")
def add_document(d: NewDoc, user: dict = Depends(admin_only)):
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
    save_meta(doc["document_id"], d.unit.strip(), 0)
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
def upload(d: Upload, user: dict = Depends(admin_only)):
    if not any(s["subject_id"] == d.subject_id for s in db.rows("Subjects")):
        raise HTTPException(400, "Subject not found")
    filename = Path(d.filename).name.strip()
    if Path(filename).suffix.lower() not in ALLOWED_EXT:
        raise HTTPException(400, "Allowed files: PDF, Word, PowerPoint, TXT")
    if len(d.data) > MAX_B64:
        raise HTTPException(400, "File is too large (max 20 MB)")
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
        raise HTTPException(502, f"Could not save to Google Drive: {str(e)[:200]}")
    pages = extract_pages(filename, raw)
    doc = db.add("Documents", {"document_id": new_id("D"), "subject_id": d.subject_id,
                               "pdf_name": title, "drive_link": link,
                               "document_type": d.document_type.strip() or "Notes",
                               "uploaded_by": user["user_id"], "uploaded_at": now()})
    doc = dict(doc)
    save_meta(doc["document_id"], d.unit.strip(), pages[-1][0] if pages else 0)
    doc["chunks"] = save_knowledge(doc["document_id"], d.subject_id, title, pages)
    return doc   # chunks == 0 means the file has no readable text (scanned PDF?)


@app.delete("/api/documents/{document_id}")
def delete_document(document_id: str, user: dict = Depends(admin_only)):
    doc = next((x for x in db.rows("Documents") if x["document_id"] == document_id), None)
    if not doc:
        raise HTTPException(404, "Document not found")
    db.delete_where("Documents", "document_id", document_id)
    db.delete_where("Knowledge", "document_id", document_id)
    db.delete_where("DocMeta", "document_id", document_id)
    return {"ok": True}


# ---- admin settings ----
@app.get("/api/admin/settings")
def get_settings(admin: dict = Depends(admin_only)):
    return {"folder_id": setting("drive_folder_id"), "folder_name": setting("drive_folder_name")}


@app.post("/api/admin/folder")
def set_folder(d: Folder, admin: dict = Depends(admin_only)):
    fid = folder_id_from(d.link)
    if not fid:
        raise HTTPException(400, "Paste a Google Drive folder link (.../drive/folders/...)")
    if not os.getenv("DRIVE_UPLOAD_URL"):
        raise HTTPException(400, "Google Drive is not connected on the server yet (see README step 2)")
    try:
        out = script_post({"action": "folder", "id": fid}, 60)
    except Exception as e:
        raise HTTPException(502, f"Apps Script not reachable ({type(e).__name__})")
    if not out.get("name"):
        raise HTTPException(400, "Folder could not be opened. Give the script owner's Google account Editor access, "
                                 "and deploy the new drive_upload.gs as a NEW VERSION. " + str(out.get("error", ""))[:150])
    for key, value in (("drive_folder_id", fid), ("drive_folder_name", out["name"])):
        db.delete_where("Settings", "key", key)
        db.add("Settings", {"key": key, "value": value})
    return {"folder_id": fid, "folder_name": out["name"]}


@app.post("/api/admin/refresh")
def refresh_data(admin: dict = Depends(admin_only)):
    db.reload()
    return {"ok": True}


# ---- AI chat ----
def subject_set(semester_id: str = "", subject_id: str = "") -> Optional[set]:
    if subject_id:
        return {subject_id}
    if semester_id:
        return {x["subject_id"] for x in db.rows("Subjects") if x["semester_id"] == semester_id}
    return None


@app.post("/api/ask")
def ask(d: Ask, user: dict = Depends(current_user)):
    q = need(d.question, "Question")
    if len(q) > 1000:
        raise HTTPException(400, "Question is too long (max 1000 characters)")
    if d.mode not in MODES:
        raise HTTPException(400, "Unknown mode")
    rows = knowledge_rows(subject_set(d.semester_id or "", d.subject_id), d.document_id, d.unit)
    result = generate(q, rows, d.mode, d.marks, d.whole and bool(d.document_id or d.subject_id))
    mid = new_id("M")
    cid = d.conversation_id if re.fullmatch(r"[\w-]{1,40}", d.conversation_id) else new_id("T")
    meta = {"cid": cid, "subject_id": d.subject_id, "sources": [{**x, "text": x["text"][:200]} for x in result["sources"]]}
    chat_queue.put(({"message_id": mid, "user_id": user["user_id"], "mode": d.mode, "question": q,
                     "answer": result["answer"], "created_at": now()}, meta))
    return {**result, "message_id": mid, "conversation_id": cid}


def my_chats(user_id: str) -> list:
    meta = {r["ref"]: item_data(r, {}) for r in db.rows("UserData") if r["kind"] == "msg" and r["user_id"] == user_id}
    out = []
    for c in db.rows("Chats"):
        if c.get("user_id") == user_id:
            m = meta.get(c["message_id"], {})
            out.append({"message_id": c["message_id"], "question": c["question"], "answer": c["answer"],
                        "mode": c["mode"], "created_at": c["created_at"], "conversation_id": m.get("cid") or "earlier",
                        "subject_id": m.get("subject_id", ""), "sources": m.get("sources", [])})
    return out


@app.get("/api/history")
def history(user: dict = Depends(current_user)):
    return my_chats(user["user_id"])[-300:]                       # oldest first


@app.delete("/api/chats/{conversation_id}")
def delete_chat(conversation_id: str, user: dict = Depends(current_user)):
    chat_queue.join()
    for c in my_chats(user["user_id"]):
        if c["conversation_id"] == conversation_id:
            db.delete_where("Chats", "message_id", c["message_id"])
            db.delete_where("UserData", "ref", c["message_id"])
    return {"ok": True}


# ---- quiz ----
@app.post("/api/quiz")
def quiz(d: Quiz, user: dict = Depends(current_user)):
    if d.difficulty not in ("easy", "medium", "hard"):
        raise HTTPException(400, "Unknown difficulty")
    rows = knowledge_rows({d.subject_id}, unit=d.unit)
    qs = quiz_items([x for r in rows for x in sentences(r["text"])], max(1, min(d.count, 30)), d.difficulty)
    if not qs:
        raise HTTPException(400, "Not enough notes for a quiz in this subject/unit yet")
    return qs


# ---- personal data: profile, bookmarks, favourites, quiz results, feedback ----
ITEM_KINDS = {"profile", "bookmark", "fav", "quiz", "feedback", "convname", "savedconv", "view",
              "task", "mynote", "focus", "exam", "slot"}
SINGLE = {"profile", "fav", "feedback", "convname", "savedconv", "view", "task", "mynote", "exam", "slot"}   # saving again replaces the old one


@app.get("/api/items")
def items(user: dict = Depends(current_user)):
    return [{"item_id": r["item_id"], "kind": r["kind"], "ref": r["ref"], "data": item_data(r, {}),
             "created_at": r["created_at"]} for r in db.rows("UserData")
            if r["user_id"] == user["user_id"] and r["kind"] in ITEM_KINDS]


@app.post("/api/items")
def save_item(d: Item, user: dict = Depends(current_user)):
    if d.kind not in ITEM_KINDS:
        raise HTTPException(400, "Unknown item")
    if len(json.dumps(d.data)) > 45_000 or len(d.ref) > 80:
        raise HTTPException(400, "Too large to save")
    if d.kind in SINGLE:
        for r in [r for r in db.rows("UserData")
                  if r["user_id"] == user["user_id"] and r["kind"] == d.kind and r["ref"] == d.ref]:
            db.delete_where("UserData", "item_id", r["item_id"])
    r = put_item(user["user_id"], d.kind, d.ref, d.data)
    return {"item_id": r["item_id"], "kind": d.kind, "ref": d.ref, "data": d.data, "created_at": r["created_at"]}


@app.delete("/api/items/{item_id}")
def delete_item(item_id: str, user: dict = Depends(current_user)):
    if not any(r["item_id"] == item_id and r["user_id"] == user["user_id"] and r["kind"] in ITEM_KINDS
               for r in db.rows("UserData")):
        raise HTTPException(404, "Not found")
    db.delete_where("UserData", "item_id", item_id)
    return {"ok": True}


@app.post("/api/password")
def change_password(d: Password, user: dict = Depends(current_user)):
    if not check_pw(d.old, password_of(user)):
        raise HTTPException(400, "Current password is wrong")
    if len(d.new) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    put_item(user["user_id"], "pw", user["user_id"], hash_pw(d.new))
    return {"ok": True}


# ---- documents: view, prefs, public data ----
def prefs() -> dict:
    return {"categories": setting("categories") or "Notes, Book, PPT, PYQ",
            "allow_download": setting("allow_download") != "no"}


@app.get("/api/prefs")
def get_prefs(user: dict = Depends(current_user)):
    return prefs()


@app.get("/api/public")
def public_data():
    return tree({})


@app.get("/api/documents/{document_id}/file")
def document_file(document_id: str, user: dict = Depends(current_user)):
    doc = next((x for x in db.rows("Documents") if x["document_id"] == document_id), None)
    if not doc:
        raise HTTPException(404, "Document not found")
    text = [{"page": page_of(k), "text": k["text"]} for k in knowledge_rows(None, document_id)]
    out = {"text": text, "data": "", "name": ""}
    if os.getenv("DRIVE_UPLOAD_URL") and drive_file_id(doc.get("drive_link", "")):
        try:
            name, raw = drive_read(doc["drive_link"])
            out.update(name=name, data=base64.b64encode(raw).decode())
        except Exception as e:
            print(f"[VIEW] {type(e).__name__}: {e}")
    return out


# ---- admin: edit / delete / students / analytics ----
LEVELS = {"course": ("Courses", "course_id", "course_name", "Semesters"),
          "semester": ("Semesters", "semester_id", "semester_name", "Subjects"),
          "subject": ("Subjects", "subject_id", "subject_name", "Documents")}


def level_row(level: str, ident: str):
    if level not in LEVELS:
        raise HTTPException(404, "Unknown")
    tab, col, label, child = LEVELS[level]
    row = next((r for r in db.rows(tab) if r[col] == ident), None)
    if not row:
        raise HTTPException(404, "Not found")
    return tab, col, label, child, row


@app.post("/api/admin/edit/{level}/{ident}")
def edit_level(level: str, ident: str, d: Rename, admin: dict = Depends(admin_only)):
    tab, col, label, _, row = level_row(level, ident)
    new = {**row, label: need(d.name, "Name")}
    if level == "subject":
        new["subject_code"] = d.code.strip()
    db.delete_where(tab, col, ident)
    db.add(tab, new)
    return new


@app.delete("/api/admin/{level}/{ident}")
def delete_level(level: str, ident: str, admin: dict = Depends(admin_only)):
    tab, col, _, child, _ = level_row(level, ident)
    if any(r[col] == ident for r in db.rows(child)):
        raise HTTPException(400, f"First delete the {child.lower()} inside it")
    db.delete_where(tab, col, ident)
    if level == "subject":
        db.delete_where("Knowledge", "subject_id", ident)
    return {"ok": True}


@app.post("/api/admin/document/{document_id}")
def edit_document(document_id: str, d: DocEdit, admin: dict = Depends(admin_only)):
    doc = next((x for x in db.rows("Documents") if x["document_id"] == document_id), None)
    if not doc:
        raise HTTPException(404, "Document not found")
    if not any(s["subject_id"] == d.subject_id for s in db.rows("Subjects")):
        raise HTTPException(400, "Subject not found")
    link = d.drive_link.strip()
    if link and not re.match(r"^https?://", link):
        raise HTTPException(400, "Link must start with http:// or https://")
    new = {**doc, "pdf_name": need(d.pdf_name, "Title"), "subject_id": d.subject_id, "drive_link": link,
           "document_type": d.document_type or "Notes"}
    chunks = [k for k in db.rows("Knowledge") if k["document_id"] == document_id]
    db.delete_where("Documents", "document_id", document_id)
    db.add("Documents", new)
    if chunks and (new["subject_id"] != doc["subject_id"] or new["pdf_name"] != doc["pdf_name"]):
        db.delete_where("Knowledge", "document_id", document_id)
        db.add_many("Knowledge", [{**k, "subject_id": d.subject_id, "title": new["pdf_name"]} for k in chunks])
    save_meta(document_id, d.unit.strip(), meta_map().get(document_id, {}).get("pages", ""))
    return new


@app.post("/api/admin/reprocess/{document_id}")
def reprocess(document_id: str, admin: dict = Depends(admin_only)):
    doc = next((x for x in db.rows("Documents") if x["document_id"] == document_id), None)
    if not doc or not drive_file_id(doc.get("drive_link", "")) or not os.getenv("DRIVE_UPLOAD_URL"):
        raise HTTPException(400, "This document has no readable Drive file")
    try:
        n = index_document(doc)
    except Exception as e:
        raise HTTPException(502, f"Could not read the file: {str(e)[:150]}")
    if not n:
        raise HTTPException(400, "No readable text in this file (scanned PDF?)")
    return {"chunks": n}


@app.get("/api/admin/overview")
def overview(admin: dict = Depends(admin_only)):
    users, chats, data = db.rows("Users"), db.rows("Chats"), db.rows("UserData")
    learned = {k["document_id"] for k in db.rows("Knowledge")}
    subj = {s["subject_id"]: s["subject_name"] for s in db.rows("Subjects")}
    by_day = Counter(c["created_at"][:10] for c in chats)
    by_subject = Counter(subj.get(item_data(r, {}).get("subject_id"), "All / other") for r in data if r["kind"] == "msg")
    return {
        "students": [{**public(u), "disabled": is_disabled(u),
                      "questions": sum(c["user_id"] == u["user_id"] for c in chats),
                      "profile": item_data(next((r for r in reversed(data) if r["kind"] == "profile"
                                                 and r["user_id"] == u["user_id"]), None), {}).get("semester_id", "")}
                     for u in users if u["role"] == "student"],
        "teachers": [{**public(u), "disabled": is_disabled(u), "subjects": teaches(u["user_id"])}
                     for u in users if u["role"] == "teacher"],
        "questions": len(chats), "quizzes": sum(r["kind"] == "quiz" for r in data),
        "processed": sorted(learned), "by_day": sorted(by_day.items())[-14:],
        "by_mode": Counter(c["mode"] for c in chats).most_common(), "by_subject": by_subject.most_common(8),
        "likes": sum(r["kind"] == "feedback" and item_data(r, {}).get("value") == 1 for r in data),
        "dislikes": sum(r["kind"] == "feedback" and item_data(r, {}).get("value") == -1 for r in data),
    }


def student_or_404(user_id: str) -> dict:
    u = find_user(user_id=user_id)
    if not u or u["role"] not in ("student", "teacher"):
        raise HTTPException(404, "User not found")
    return u


@app.post("/api/admin/student/{user_id}/status")
def student_status(user_id: str, d: Status, admin: dict = Depends(admin_only)):
    student_or_404(user_id)
    put_item(user_id, "disabled", user_id, d.disabled)
    return {"ok": True}


@app.post("/api/admin/student/{user_id}/password")
def student_password(user_id: str, d: Password, admin: dict = Depends(admin_only)):
    student_or_404(user_id)
    if len(d.new) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    put_item(user_id, "pw", user_id, hash_pw(d.new))
    return {"ok": True}


# ---- teachers ----
class Teacher(BaseModel):
    name: str
    email: str
    password: str
    subjects: list = []


class Subjects(BaseModel):
    subjects: list = []


def teaches(user_id: str) -> list:
    return item_data(latest("teaches", user_id), [])


def valid_subjects(ids: list) -> list:
    known = {s["subject_id"] for s in db.rows("Subjects")}
    return [i for i in dict.fromkeys(ids) if i in known]


@app.post("/api/admin/teacher")
def add_teacher(d: Teacher, admin: dict = Depends(admin_only)):
    email, name = d.email.strip().lower(), need(d.name, "Name")
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "Enter a valid email")
    if len(d.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if find_user(email=email):
        raise HTTPException(400, "Email already registered")
    user = db.add("Users", {"user_id": new_id("U"), "name": name, "email": email,
                            "password_hash": hash_pw(d.password), "role": "teacher",
                            "status": "active", "created_at": now()})
    put_item(user["user_id"], "teaches", user["user_id"], valid_subjects(d.subjects))
    return public(user)


@app.post("/api/admin/teacher/{user_id}/subjects")
def teacher_subjects(user_id: str, d: Subjects, admin: dict = Depends(admin_only)):
    u = find_user(user_id=user_id)
    if not u or u["role"] != "teacher":
        raise HTTPException(404, "Teacher not found")
    put_item(user_id, "teaches", user_id, valid_subjects(d.subjects))
    return {"ok": True}


@app.get("/api/teaching")
def teaching(user: dict = Depends(current_user)):
    return teaches(user["user_id"])


# ---- assignments ----
class Assignment(BaseModel):
    subject_id: str
    title: str
    description: str
    due: str
    marks: int = 10


class Submission(BaseModel):
    text: str = ""
    link: str = ""


class Grade(BaseModel):
    student_id: str
    marks: float
    feedback: str = ""


def staff_only(user: dict = Depends(current_user)) -> dict:
    if user["role"] not in ("admin", "teacher"):
        raise HTTPException(403, "Teachers only")
    return user


def may_manage(user: dict, subject_id: str) -> bool:
    return user["role"] == "admin" or (user["role"] == "teacher" and subject_id in teaches(user["user_id"]))


def assignment_row(assignment_id: str) -> dict:
    row = next((r for r in db.rows("UserData") if r["item_id"] == assignment_id and r["kind"] == "assign"), None)
    if not row:
        raise HTTPException(404, "Assignment not found")
    return row


def managed_assignment(assignment_id: str, user: dict) -> dict:
    row = assignment_row(assignment_id)
    if not may_manage(user, row["ref"]):
        raise HTTPException(403, "This subject is not assigned to you")
    return row


def submissions_of(assignment_id: str) -> dict:
    """student_id -> latest submission and grade."""
    out = {}
    for r in db.rows("UserData"):
        if r["kind"] == "submit" and r["ref"] == assignment_id:
            out[r["user_id"]] = {**item_data(r, {}), "submitted_at": r["created_at"], "grade": None}
    for r in db.rows("UserData"):
        if r["kind"] == "grade" and r["ref"] == assignment_id:
            g = item_data(r, {})
            if g.get("student_id") in out:
                out[g["student_id"]]["grade"] = g
    return out


@app.get("/api/assignments")
def assignments(user: dict = Depends(current_user)):
    known = {s["subject_id"] for s in db.rows("Subjects")}
    out = []
    for r in db.rows("UserData"):
        if r["kind"] != "assign" or r["ref"] not in known:
            continue
        a = {**item_data(r, {}), "id": r["item_id"], "subject_id": r["ref"], "created_at": r["created_at"]}
        if user["role"] == "student":
            a["mine"] = submissions_of(r["item_id"]).get(user["user_id"])
        elif may_manage(user, r["ref"]):
            subs = submissions_of(r["item_id"])
            a["submitted"], a["graded"] = len(subs), sum(bool(s["grade"]) for s in subs.values())
        else:
            continue
        out.append(a)
    return out


@app.post("/api/assignments")
def add_assignment(d: Assignment, user: dict = Depends(staff_only)):
    if d.subject_id not in valid_subjects([d.subject_id]) or not may_manage(user, d.subject_id):
        raise HTTPException(403, "This subject is not assigned to you")
    title, text = need(d.title, "Title"), need(d.description, "Question")
    if len(title) > 200 or len(text) > 10000:
        raise HTTPException(400, "Assignment is too long")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.due):
        raise HTTPException(400, "Choose a due date")
    if not 1 <= d.marks <= 1000:
        raise HTTPException(400, "Marks must be between 1 and 1000")
    put_item(user["user_id"], "assign", d.subject_id,
             {"title": title, "description": text, "due": d.due, "marks": d.marks, "teacher": user["name"]})
    return {"ok": True}


@app.delete("/api/assignments/{assignment_id}")
def delete_assignment(assignment_id: str, user: dict = Depends(staff_only)):
    managed_assignment(assignment_id, user)
    db.delete_where("UserData", "ref", assignment_id)       # submissions and grades
    db.delete_where("UserData", "item_id", assignment_id)
    return {"ok": True}


@app.post("/api/assignments/{assignment_id}/submit")
def submit_assignment(assignment_id: str, d: Submission, user: dict = Depends(current_user)):
    if user["role"] != "student":
        raise HTTPException(403, "Only students can submit")
    assignment_row(assignment_id)
    text, link = d.text.strip(), d.link.strip()
    if not text and not link:
        raise HTTPException(400, "Write an answer or paste a link")
    if len(text) > 20000 or len(link) > 500:
        raise HTTPException(400, "Answer is too long")
    if link and not re.match(r"^https?://", link):
        raise HTTPException(400, "Link must start with http:// or https://")
    if (submissions_of(assignment_id).get(user["user_id"]) or {}).get("grade"):
        raise HTTPException(400, "Already checked - it cannot be changed")
    put_item(user["user_id"], "submit", assignment_id, {"text": text, "link": link})
    return {"ok": True}


@app.get("/api/assignments/{assignment_id}/submissions")
def list_submissions(assignment_id: str, user: dict = Depends(staff_only)):
    managed_assignment(assignment_id, user)
    people = {u["user_id"]: u for u in db.rows("Users")}
    return [{"student_id": k, "name": people.get(k, {}).get("name", "?"), "email": people.get(k, {}).get("email", ""), **v}
            for k, v in submissions_of(assignment_id).items()]


@app.post("/api/assignments/{assignment_id}/grade")
def grade_submission(assignment_id: str, d: Grade, user: dict = Depends(staff_only)):
    a = item_data(managed_assignment(assignment_id, user), {})
    if d.student_id not in submissions_of(assignment_id):
        raise HTTPException(404, "No submission from this student")
    if not 0 <= d.marks <= a.get("marks", 0):
        raise HTTPException(400, f"Marks must be between 0 and {a.get('marks')}")
    put_item(user["user_id"], "grade", assignment_id,
             {"student_id": d.student_id, "marks": d.marks, "feedback": d.feedback.strip()[:2000]})
    return {"ok": True}


# ---- leaderboard ----
@app.get("/api/leaderboard")
def leaderboard(user: dict = Depends(current_user)):
    """Points = correct answers in each student's BEST attempt per subject, so repeating a quiz adds nothing."""
    best = {}
    for r in db.rows("UserData"):
        if r["kind"] == "quiz":
            q = item_data(r, {})
            key = (r["user_id"], r["ref"])
            try:
                best[key] = max(best.get(key, 0), min(int(q.get("correct", 0)), 30))
            except (TypeError, ValueError):
                pass
    total = Counter()
    for (uid, _), points in best.items():
        total[uid] += points
    names = {u["user_id"]: u["name"] for u in db.rows("Users") if u["role"] == "student" and not is_disabled(u)}
    ranked = [(uid, p) for uid, p in total.most_common() if uid in names]
    return [{"rank": i, "name": names[uid], "points": p, "me": uid == user["user_id"]}
            for i, (uid, p) in enumerate(ranked, 1) if i <= 20 or uid == user["user_id"]]


# ---- forgot password: 6 digit code by email (sent by your Apps Script) ----
OTP = {}                    # email -> {hash, expires, tries, sent[]}   (kept in memory only)
OTP_LOCK = threading.Lock()


class Forgot(BaseModel):
    email: str


class Reset(BaseModel):
    email: str
    code: str
    new: str


def send_mail(to: str, subject: str, body: str) -> None:
    out = script_post({"action": "mail", "to": to, "subject": subject, "body": body}, 60)
    if not out.get("sent"):
        raise RuntimeError(out.get("error", "mail not sent - deploy the new drive_upload.gs"))


def mail_code(email: str, code: str) -> None:
    try:
        send_mail(email, "Sanvyara AI password reset code",
                  f"Your password reset code is {code}\n\nIt works for 10 minutes. If you did not ask for it, "
                  f"ignore this email.\n\n— {FOOTER}")
    except Exception as e:
        print(f"[MAIL] {type(e).__name__}: {e}")


@app.post("/api/forgot")
def forgot(d: Forgot):
    if not os.getenv("DRIVE_UPLOAD_URL"):
        raise HTTPException(400, "Email is not set up on the server. Please ask your admin to reset the password.")
    email = d.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "Enter a valid email")
    user = find_user(email=email)
    if user and not is_disabled(user):
        with OTP_LOCK:
            old = OTP.get(email, {})
            sent = [t for t in old.get("sent", []) if time.time() - t < 3600]
            if len(sent) < 4 and (not sent or time.time() - sent[-1] > 60):    # protects the daily mail limit
                code = f"{secrets.randbelow(1_000_000):06d}"
                OTP[email] = {"hash": _sign(email + code), "expires": time.time() + 600, "tries": 0,
                              "sent": sent + [time.time()]}
                threading.Thread(target=mail_code, args=(email, code), daemon=True).start()
    # same reply whether or not the email exists
    return {"message": "If this email is registered, a code has been sent. Check inbox and spam."}


@app.post("/api/reset")
def reset_password(d: Reset):
    email = d.email.strip().lower()
    if len(d.new) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    with OTP_LOCK:
        entry = OTP.get(email)
        ok = bool(entry) and entry["expires"] > time.time() and entry["tries"] < 5
        if ok:
            entry["tries"] += 1
            ok = hmac.compare_digest(entry["hash"], _sign(email + d.code.strip()))
        if not ok:
            raise HTTPException(400, "Wrong or expired code")
        entry["expires"] = 0                      # one-time use
    user = find_user(email=email)
    if not user or is_disabled(user):
        raise HTTPException(400, "Wrong or expired code")
    put_item(user["user_id"], "pw", user["user_id"], hash_pw(d.new))
    return {"ok": True}


@app.post("/api/admin/prefs")
def set_prefs(d: Prefs, admin: dict = Depends(admin_only)):
    cats = ", ".join(dict.fromkeys(c.strip() for c in d.categories.split(",") if c.strip()))
    for key, value in (("categories", cats), ("allow_download", "yes" if d.allow_download else "no")):
        db.delete_where("Settings", "key", key)
        db.add("Settings", {"key": key, "value": value})
    return prefs()
