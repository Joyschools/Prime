from __future__ import annotations
import re

import csv
import json
import io
import base64
import os
import sqlite3
import uuid
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import MutableMapping
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterable

from flask import (
    Flask,
    abort,
    flash as flask_flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session as flask_session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.utils import secure_filename
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
import qrcode
from PIL import Image
from pypdf import PdfReader, PdfWriter

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
_render_data_dir = Path(os.environ.get("PERSISTENT_DATA_DIR", "/var/data"))
if os.environ.get("DATA_DIR"):
    DATA_DIR = Path(os.environ["DATA_DIR"]).expanduser()
elif os.environ.get("RENDER") and _render_data_dir.exists():
    DATA_DIR = _render_data_dir
else:
    DATA_DIR = INSTANCE_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "school.db"
SECRET_FILE = DATA_DIR / "secret.key"
UPLOAD_DIR = DATA_DIR / "uploads"
PERSISTENT_STORAGE = DATA_DIR != INSTANCE_DIR
PULSE_PEER_URL = os.environ.get("PULSE_PEER_URL", "https://breathe-xozy.onrender.com").strip().rstrip("/")
PULSE_TIMEOUT_SECONDS = max(1, min(10, int(os.environ.get("PULSE_TIMEOUT_SECONDS", "4"))))
PULSE_ALLOWED_CALLBACK_HOSTS = {h.strip().lower() for h in os.environ.get("PULSE_ALLOWED_CALLBACK_HOSTS", "").split(",") if h.strip()}
ALLOWED_RESTORE_EXT = {"db", "sqlite", "sqlite3"}
PUBLIC_ROLES = ("Teacher", "Student", "Parent")
HIDDEN_ROLES = ("Admin", "ICT", "Finance", "Librarian")
ALL_PORTAL_ROLES = HIDDEN_ROLES + PUBLIC_ROLES
ADMIN_LOGIN_PATH = "/xtspolsjhulupjoppsup-lmkzcodup"
ADMIN_ROLES = {"Admin"}
ALL_ROLES = ALL_PORTAL_ROLES
SYSTEM_ROLE = "System"
INSTITUTION_TYPES = ("Primary School", "Secondary School", "TVET", "College", "University", "Mixed Institution")
DEFAULT_DEPARTMENTS = ("Communications", "Computer Studies")
_PORTAL_ROLE_COOKIE = "school_portal_role"
_AUTH_COOKIE = "school_auth_token"
_AUTH_COOKIE_MAX_AGE = 60 * 60 * 24 * 30

app = Flask(__name__, instance_path=str(INSTANCE_DIR), instance_relative_config=True)
app.config.update(
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,
    TEMPLATES_AUTO_RELOAD=True,
)

# -------------------------------------------------------------------
# Signed authentication session
# -------------------------------------------------------------------
def _load_or_create_secret() -> str:
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    if SECRET_FILE.exists():
        value = SECRET_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    legacy_secret = INSTANCE_DIR / "secret.key"
    if SECRET_FILE != legacy_secret and legacy_secret.exists():
        value = legacy_secret.read_text(encoding="utf-8").strip()
        if value:
            SECRET_FILE.write_text(value, encoding="utf-8")
            return value
    value = uuid.uuid4().hex + uuid.uuid4().hex
    try:
        fd = os.open(str(SECRET_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value)
        return value
    except FileExistsError:
        # Another Gunicorn worker created the shared secret at the same time.
        return SECRET_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        SECRET_FILE.write_text(value, encoding="utf-8")
        return value

_secret_key = _load_or_create_secret()
app.config.update(
    SECRET_KEY=_secret_key,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("COOKIE_SECURE") == "1" or os.environ.get("RENDER")),
    SESSION_COOKIE_NAME="school_portal_session",
    SESSION_COOKIE_PATH="/",
    SESSION_REFRESH_EACH_REQUEST=True,
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
)
session = flask_session

def flash(message: str, category: str = "message") -> None:
    flask_flash(message, category)


UPLOAD_DIR.mkdir(exist_ok=True)
DOC_DIR = UPLOAD_DIR / "documents"
DOC_DIR.mkdir(exist_ok=True)
QR_DIR = UPLOAD_DIR / "qr"
QR_DIR.mkdir(exist_ok=True)


# -------------------------
# Database helpers
# -------------------------
def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_: Any) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def ensure_column(conn: sqlite3.Connection, table: str, column_def: str) -> None:
    name = column_def.split()[0]
    if name not in table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")


def init_db() -> None:
    INSTANCE_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    legacy_db = INSTANCE_DIR / "school.db"
    if PERSISTENT_STORAGE and not DB_PATH.exists() and legacy_db.exists():
        import shutil
        shutil.copy2(legacy_db, DB_PATH)
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('Admin','ICT','Finance','Teacher','Student','Parent','Librarian','System')),
                student_id INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS school_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                school_name TEXT NOT NULL DEFAULT 'School',
                admission_prefix TEXT NOT NULL DEFAULT 'ADM-',
                admission_suffix TEXT NOT NULL DEFAULT '',
                student_name_prefix TEXT NOT NULL DEFAULT '',
                student_name_suffix TEXT NOT NULL DEFAULT '',
                currency_code TEXT NOT NULL DEFAULT 'KES',
                school_fee REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admission_no TEXT NOT NULL UNIQUE,
                full_name TEXT NOT NULL,
                grade TEXT NOT NULL,
                guardian_name TEXT,
                guardian_phone TEXT,
                guardian_email TEXT,
                alt_guardian_name TEXT,
                alt_guardian_phone TEXT,
                alt_guardian_email TEXT,
                student_phone TEXT,
                student_email TEXT,
                medical_condition TEXT,
                allergies TEXT,
                special_info TEXT,
                notes TEXT,
                payment_status TEXT NOT NULL DEFAULT 'Pending' CHECK(payment_status IN ('Paid', 'Pending')),
                balance REAL NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                method TEXT NOT NULL,
                reference_no TEXT,
                recorded_by INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'Posted' CHECK(status IN ('Posted', 'Reversed')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reversed_at TEXT,
                FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
                FOREIGN KEY(recorded_by) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER,
                actor_name TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(actor_id) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS exam_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                grade TEXT NOT NULL,
                term TEXT NOT NULL,
                submitted_by INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'Submitted',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(submitted_by) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS exam_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                grade TEXT NOT NULL,
                term TEXT NOT NULL,
                subject TEXT NOT NULL,
                student_id INTEGER NOT NULL,
                student_name TEXT NOT NULL,
                admission_no TEXT NOT NULL,
                mark REAL NOT NULL,
                max_mark REAL NOT NULL DEFAULT 100,
                submitted_by INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(batch_id) REFERENCES exam_batches(id) ON DELETE CASCADE,
                FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
                FOREIGN KEY(submitted_by) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                subject TEXT NOT NULL,
                grade TEXT NOT NULL,
                description TEXT,
                deadline TEXT,
                attachment_path TEXT,
                posted_by INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assignment_id INTEGER NOT NULL,
                student_id INTEGER NOT NULL,
                attachment_path TEXT,
                note TEXT,
                score REAL,
                feedback TEXT,
                submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(assignment_id) REFERENCES assignments(id) ON DELETE CASCADE,
                FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS portal_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_role TEXT NOT NULL,
                sender_name TEXT NOT NULL,
                recipient_role TEXT NOT NULL,
                recipient_student_id INTEGER,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS portal_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_type TEXT NOT NULL CHECK(document_type IN ('Result', 'Exam Card')),
                student_id INTEGER NOT NULL,
                batch_id INTEGER,
                file_path TEXT,
                qr_token TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'Valid' CHECK(status IN ('Valid', 'Revoked')),
                issued_by INTEGER,
                issued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
                FOREIGN KEY(batch_id) REFERENCES exam_batches(id) ON DELETE SET NULL,
                FOREIGN KEY(issued_by) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS elections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                start_at TEXT,
                end_at TEXT,
                visible INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 0,
                created_by INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS election_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                election_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                position TEXT NOT NULL,
                manifesto TEXT,
                image_path TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(election_id) REFERENCES elections(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS election_votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                election_id INTEGER NOT NULL,
                candidate_id INTEGER NOT NULL,
                voter_user_id INTEGER NOT NULL,
                position TEXT NOT NULL DEFAULT 'General',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(election_id, voter_user_id, position),
                FOREIGN KEY(election_id) REFERENCES elections(id) ON DELETE CASCADE,
                FOREIGN KEY(candidate_id) REFERENCES election_candidates(id) ON DELETE CASCADE,
                FOREIGN KEY(voter_user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS library_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'Book',
                author TEXT,
                item_code TEXT,
                quantity INTEGER NOT NULL DEFAULT 1,
                available_quantity INTEGER NOT NULL DEFAULT 1,
                location TEXT,
                resource_type TEXT NOT NULL DEFAULT 'Physical',
                file_path TEXT,
                external_url TEXT,
                description TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS library_loans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                student_id INTEGER NOT NULL,
                issued_by INTEGER NOT NULL,
                issued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                due_date TEXT,
                returned_at TEXT,
                status TEXT NOT NULL DEFAULT 'Issued' CHECK(status IN ('Issued','Returned','Overdue')),
                FOREIGN KEY(item_id) REFERENCES library_items(id) ON DELETE CASCADE,
                FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
                FOREIGN KEY(issued_by) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS departments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL DEFAULT 'Academic',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS guardian_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guardian_user_id INTEGER NOT NULL,
                student_id INTEGER NOT NULL,
                relationship TEXT NOT NULL DEFAULT 'Guardian',
                is_primary INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guardian_user_id, student_id),
                FOREIGN KEY(guardian_user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE
            );
            """
        )

        ensure_column(conn, "school_settings", "school_name TEXT NOT NULL DEFAULT 'School'")
        ensure_column(conn, "school_settings", "admission_prefix TEXT NOT NULL DEFAULT 'ADM-'")
        ensure_column(conn, "school_settings", "admission_suffix TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "school_settings", "student_name_prefix TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "school_settings", "student_name_suffix TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "school_settings", "currency_code TEXT NOT NULL DEFAULT 'KES'")
        ensure_column(conn, "school_settings", "school_fee REAL NOT NULL DEFAULT 0")
        ensure_column(conn, "school_settings", "menu_order TEXT NOT NULL DEFAULT 'Home,Academics,Assignments,Messages,Profile'")
        ensure_column(conn, "school_settings", "background_path TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "school_settings", "logo_path TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "school_settings", "portal_subtitle TEXT NOT NULL DEFAULT 'School Portal System'")
        ensure_column(conn, "school_settings", "panel_color TEXT NOT NULL DEFAULT '#40414f'")
        ensure_column(conn, "school_settings", "background_color TEXT NOT NULL DEFAULT '#343541'")
        ensure_column(conn, "school_settings", "accent_color TEXT NOT NULL DEFAULT '#0e8a6d'")
        ensure_column(conn, "school_settings", "primary_color TEXT NOT NULL DEFAULT '#10a37f'")
        ensure_column(conn, "school_settings", "branding_label TEXT NOT NULL DEFAULT 'Branding'")
        ensure_column(conn, "school_settings", "finance_label TEXT NOT NULL DEFAULT 'Finance'")
        ensure_column(conn, "school_settings", "messages_label TEXT NOT NULL DEFAULT 'Messages'")
        ensure_column(conn, "school_settings", "results_label TEXT NOT NULL DEFAULT 'Results'")
        ensure_column(conn, "school_settings", "assignments_label TEXT NOT NULL DEFAULT 'Assignments'")
        ensure_column(conn, "school_settings", "home_label TEXT NOT NULL DEFAULT 'Home'")
        ensure_column(conn, "school_settings", "result_download_balance_limit REAL NOT NULL DEFAULT 500")
        ensure_column(conn, "school_settings", "result_release_mode TEXT NOT NULL DEFAULT 'Finance Approval'")
        ensure_column(conn, "school_settings", "exam_card_enabled INTEGER NOT NULL DEFAULT 1")
        ensure_column(conn, "school_settings", "auth_required INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "school_settings", "prelogin_sections TEXT NOT NULL DEFAULT 'institution,history,achievements,owners,developer,company'")
        ensure_column(conn, "school_settings", "institution_owners TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "school_settings", "developer_name TEXT NOT NULL DEFAULT 'Toror Technology and Innovations Ltd.'")
        ensure_column(conn, "school_settings", "developer_about TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "school_settings", "company_name TEXT NOT NULL DEFAULT 'Toror Technology and Innovations Ltd.'")
        ensure_column(conn, "school_settings", "company_about TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "school_settings", "elections_enabled INTEGER NOT NULL DEFAULT 1")
        ensure_column(conn, "school_settings", "library_enabled INTEGER NOT NULL DEFAULT 1")
        ensure_column(conn, "school_settings", "institution_enabled INTEGER NOT NULL DEFAULT 1")
        ensure_column(conn, "school_settings", "institution_history TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "school_settings", "institution_performance TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "school_settings", "institution_religion TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "school_settings", "institution_affiliations TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "school_settings", "institution_help TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "school_settings", "institution_contact TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "school_settings", "institution_image_path TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "school_settings", "parent_portal_enabled INTEGER NOT NULL DEFAULT 1")

        ensure_column(conn, "students", "guardian_name TEXT")
        ensure_column(conn, "students", "guardian_phone TEXT")
        ensure_column(conn, "students", "guardian_email TEXT")
        ensure_column(conn, "students", "alt_guardian_name TEXT")
        ensure_column(conn, "students", "alt_guardian_phone TEXT")
        ensure_column(conn, "students", "alt_guardian_email TEXT")
        ensure_column(conn, "students", "student_phone TEXT")
        ensure_column(conn, "students", "student_email TEXT")
        ensure_column(conn, "students", "medical_condition TEXT")
        ensure_column(conn, "students", "allergies TEXT")
        ensure_column(conn, "students", "special_info TEXT")
        ensure_column(conn, "payments", "receipt_path TEXT")
        ensure_column(conn, "students", "notes TEXT")
        ensure_column(conn, "students", "date_of_birth TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "students", "gender TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "students", "id_reference TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "students", "address TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "students", "emergency_contact TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "students", "blood_group TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "students", "medical_notes TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "students", "accountability_notes TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "exam_batches", "finance_status TEXT NOT NULL DEFAULT 'Pending'")
        ensure_column(conn, "exam_batches", "finance_note TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "exam_batches", "approved_by INTEGER")
        ensure_column(conn, "exam_batches", "approved_at TEXT")
        if "election_votes" in [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]:
            ensure_column(conn, "election_votes", "position TEXT NOT NULL DEFAULT 'General'")

        if conn.execute("SELECT COUNT(*) AS c FROM school_settings").fetchone()["c"] == 0:
            conn.execute(
                "INSERT INTO school_settings(id, school_name, admission_prefix, admission_suffix, student_name_prefix, student_name_suffix, currency_code, school_fee) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
                ("School Portal System", "ADM-", "", "", "", "KES", 0),
            )
        else:
            conn.execute("UPDATE school_settings SET school_name = 'School Portal System' WHERE id = 1 AND TRIM(school_name) IN ('', 'School', 'Legacy Portal')")

        # Keep the two baseline college/TVET departments available immediately.
        for dept_name in DEFAULT_DEPARTMENTS:
            conn.execute("INSERT OR IGNORE INTO departments(name, category) VALUES(?, 'Academic')", (dept_name,))

        # Extend the users role constraint to support the dedicated Librarian role.
        # Rebuild the user table and its direct FK dependants together. SQLite can
        # otherwise leave stale FK metadata pointing at a temporary migration table.
        cols = table_columns(conn, "users")
        if cols and "role" in cols:
            table_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
            sql_text = (table_sql[0] or "") if table_sql else ""
            if "'Librarian'" not in sql_text:
                conn.execute("PRAGMA foreign_keys=OFF")
                for table, tmp, create_sql, columns in [
                    ("payments", "payments_user_roles_fix", """CREATE TABLE {tmp} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL, amount REAL NOT NULL,
                        method TEXT NOT NULL, reference_no TEXT, recorded_by INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT 'Posted' CHECK(status IN ('Posted','Reversed')),
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, reversed_at TEXT, receipt_path TEXT,
                        FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
                        FOREIGN KEY(recorded_by) REFERENCES users_new(id) ON DELETE CASCADE
                    )""",
                     ["id","student_id","amount","method","reference_no","recorded_by","status","created_at","reversed_at","receipt_path"]),
                    ("audit_log", "audit_log_user_roles_fix", """CREATE TABLE {tmp} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER, actor_name TEXT NOT NULL,
                        action TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(actor_id) REFERENCES users_new(id) ON DELETE SET NULL
                    )""",
                     ["id","actor_id","actor_name","action","details","created_at"]),
                ]:
                    conn.execute(f"DROP TABLE IF EXISTS {tmp}")
                conn.execute("ALTER TABLE users RENAME TO users_legacy_roles")
                conn.execute("""CREATE TABLE users_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, full_name TEXT NOT NULL, username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('Admin','ICT','Finance','Teacher','Student','Parent','Librarian','System')),
                    student_id INTEGER, active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE SET NULL
                )""")
                conn.execute("INSERT INTO users_new(id,full_name,username,password_hash,role,student_id,active,created_at) SELECT id,full_name,username,password_hash,role,student_id,active,created_at FROM users_legacy_roles")
                for table, tmp, create_sql, columns in [
                    ("payments", "payments_user_roles_fix", """CREATE TABLE {tmp} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL, amount REAL NOT NULL,
                        method TEXT NOT NULL, reference_no TEXT, recorded_by INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT 'Posted' CHECK(status IN ('Posted','Reversed')),
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, reversed_at TEXT, receipt_path TEXT,
                        FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
                        FOREIGN KEY(recorded_by) REFERENCES users_new(id) ON DELETE CASCADE
                    )""",
                     ["id","student_id","amount","method","reference_no","recorded_by","status","created_at","reversed_at","receipt_path"]),
                    ("audit_log", "audit_log_user_roles_fix", """CREATE TABLE {tmp} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER, actor_name TEXT NOT NULL,
                        action TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(actor_id) REFERENCES users_new(id) ON DELETE SET NULL
                    )""",
                     ["id","actor_id","actor_name","action","details","created_at"]),
                ]:
                    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                    if exists:
                        conn.execute(create_sql.format(tmp=tmp))
                        conn.execute(f"INSERT INTO {tmp} ({','.join(columns)}) SELECT {','.join(columns)} FROM {table}")
                        conn.execute(f"DROP TABLE {table}")
                        conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")
                conn.execute("DROP TABLE users_legacy_roles")
                conn.execute("ALTER TABLE users_new RENAME TO users")
                conn.execute("PRAGMA foreign_keys=ON")

        # Authentication is account-based. A fresh installation starts with only a
        # non-login system account so existing demo payments/audit records remain valid.
        # The first real Administrator is created through /register.
        if "student_id" not in table_columns(conn, "users") or "active" not in table_columns(conn, "users"):
            # Migrate older users table while preserving existing rows.
            old_rows = conn.execute("SELECT id, full_name, username, password_hash, role, created_at FROM users").fetchall()
            conn.execute("ALTER TABLE users RENAME TO users_legacy")
            conn.execute("""CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('Admin','ICT','Finance','Teacher','Student','Parent','Librarian','System')),
                student_id INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE SET NULL
            )""")
            for r in old_rows:
                role = r[4] if r[4] in ALL_PORTAL_ROLES else SYSTEM_ROLE
                conn.execute("INSERT INTO users(id,full_name,username,password_hash,role,active,created_at) VALUES(?,?,?,?,?,?,?)",
                             (r[0],r[1],r[2],r[3],role,1,r[5] or datetime.utcnow().isoformat(timespec="seconds")))
            conn.execute("DROP TABLE users_legacy")
        ensure_column(conn, "users", "student_id INTEGER")
        ensure_column(conn, "users", "active INTEGER NOT NULL DEFAULT 1")
        ensure_column(conn, "users", "title TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "users", "department TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "users", "phone TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "users", "email TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "users", "date_of_birth TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "users", "gender TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "users", "id_reference TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "users", "address TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "users", "emergency_contact TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "users", "blood_group TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "users", "medical_notes TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "users", "accountability_notes TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "users", "profile_photo TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "users", "archived_at TEXT")
        ensure_column(conn, "school_settings", "auth_initialized INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "school_settings", "institution_type TEXT NOT NULL DEFAULT 'Secondary School'")
        ensure_column(conn, "school_settings", "learner_label TEXT NOT NULL DEFAULT 'Student'")
        ensure_column(conn, "school_settings", "staff_label TEXT NOT NULL DEFAULT 'Teacher'")
        ensure_column(conn, "school_settings", "academic_period_label TEXT NOT NULL DEFAULT 'Term'")
        ensure_column(conn, "school_settings", "class_label TEXT NOT NULL DEFAULT 'Class'")
        ensure_column(conn, "school_settings", "department_label TEXT NOT NULL DEFAULT 'Department'")
        ensure_column(conn, "school_settings", "theme_mode TEXT NOT NULL DEFAULT 'dark'")
        ensure_column(conn, "school_settings", "font_family TEXT NOT NULL DEFAULT 'Inter'")
        ensure_column(conn, "school_settings", "heading_font TEXT NOT NULL DEFAULT 'Inter'")
        ensure_column(conn, "school_settings", "radius_px INTEGER NOT NULL DEFAULT 12")
        ensure_column(conn, "school_settings", "sidebar_color TEXT NOT NULL DEFAULT '#40414f'")
        ensure_column(conn, "school_settings", "header_color TEXT NOT NULL DEFAULT '#40414f'")
        ensure_column(conn, "school_settings", "text_color TEXT NOT NULL DEFAULT '#ececf1'")
        ensure_column(conn, "school_settings", "muted_text_color TEXT NOT NULL DEFAULT '#b5bac7'")
        ensure_column(conn, "school_settings", "button_radius_px INTEGER NOT NULL DEFAULT 10")
        ensure_column(conn, "school_settings", "sidebar_style TEXT NOT NULL DEFAULT 'drawer'")
        ensure_column(conn, "school_settings", "custom_css TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "school_settings", "ai_enabled INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "school_settings", "ai_provider TEXT NOT NULL DEFAULT 'openai_responses'")
        ensure_column(conn, "school_settings", "ai_model TEXT NOT NULL DEFAULT 'gpt-5.6'")
        ensure_column(conn, "school_settings", "help_phone TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "school_settings", "help_email TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "users", "qr_access_token TEXT")
        ensure_column(conn, "users", "position_code TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "library_items", "class_level TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "library_items", "subject TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "library_items", "image_path TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "library_items", "youtube_url TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "library_items", "source_url TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "library_items", "source_name TEXT NOT NULL DEFAULT ''")

        conn.executescript("""
        CREATE TABLE IF NOT EXISTS system_help (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'Getting Started',
            content TEXT NOT NULL,
            role_scope TEXT NOT NULL DEFAULT 'All',
            sort_order INTEGER NOT NULL DEFAULT 100,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ai_usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            provider TEXT NOT NULL,
            model TEXT,
            prompt_preview TEXT,
            response_preview TEXT,
            status TEXT NOT NULL DEFAULT 'Success',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS student_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            author_user_id INTEGER NOT NULL,
            category TEXT NOT NULL DEFAULT 'General',
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            visible_to_parent INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY(author_user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_student_records_student ON student_records(student_id, created_at);
        CREATE TABLE IF NOT EXISTS finance_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_type TEXT NOT NULL CHECK(entry_type IN ('Income','Expense','Payroll','Adjustment')),
            category TEXT NOT NULL,
            payee_user_id INTEGER,
            amount REAL NOT NULL CHECK(amount > 0),
            description TEXT NOT NULL,
            reference_no TEXT,
            status TEXT NOT NULL DEFAULT 'Posted' CHECK(status IN ('Posted','Reversed')),
            posted_by INTEGER NOT NULL,
            posted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reversed_by INTEGER,
            reversed_at TEXT,
            FOREIGN KEY(payee_user_id) REFERENCES users(id) ON DELETE SET NULL,
            FOREIGN KEY(posted_by) REFERENCES users(id) ON DELETE RESTRICT,
            FOREIGN KEY(reversed_by) REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS finance_accounts (
            id INTEGER PRIMARY KEY CHECK(id=1),
            account_name TEXT NOT NULL DEFAULT 'Institution Operating Account',
            opening_balance REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS backup_registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            backup_type TEXT NOT NULL,
            file_name TEXT NOT NULL,
            row_count INTEGER NOT NULL DEFAULT 0,
            created_by INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_library_class ON library_items(class_level, subject, active);
        CREATE INDEX IF NOT EXISTS idx_users_role_active ON users(role, active);
        CREATE INDEX IF NOT EXISTS idx_finance_status_date ON finance_ledger(status, posted_at);
        """)
        conn.execute("INSERT OR IGNORE INTO finance_accounts(id, account_name, opening_balance) VALUES(1, 'Institution Operating Account', 0)")
        conn.execute("UPDATE users SET qr_access_token=lower(hex(randomblob(16))) WHERE role!='System' AND (qr_access_token IS NULL OR qr_access_token='')")
        if conn.execute("SELECT COUNT(*) FROM system_help").fetchone()[0] == 0:
            help_rows=[
                ('Getting started','Getting Started','Use the navigation to open your workspace. Administrators manage people, institution settings, security, backups and permissions.','All',10),
                ('Managing people','People & Access','Open People & Access to create, edit, archive and restore accounts. Role controls the security boundary; title and department describe institutional responsibility.','Admin',20),
                ('Learner records','Learners','Learner profiles contain identity, class/grade, guardian links, contact details, accountability information, academic and finance references.','All',30),
                ('Finance posting','Finance','Finance officers can post institutional income, expenses and payroll. Posted transactions are immutable; only an Administrator may reverse one.','Finance,Admin',40),
                ('Library resources','Library','Library staff can catalogue books, past papers, notes, images, uploaded documents, YouTube resources and external websites and assign resources to classes or subjects.','Teacher,Student,Parent,Librarian,Admin,ICT',50),
                ('Backup and recovery','Administration','A full JSON backup contains database records, system settings and available uploaded assets. Restore it through Administration > Backup & Recovery. Keep an additional SQLite backup for fast rollback.','Admin',60),
                ('AI assistant','AI','AI features require an OpenAI API key on the server environment. The assistant uses the configured provider/model and records a small audit preview without storing API keys in the database.','All',70),
            ]
            conn.executemany('INSERT INTO system_help(title,category,content,role_scope,sort_order) VALUES(?,?,?,?,?)',help_rows)

        # Convert legacy seeded accounts to an inert System account on this build.
        auth_row = conn.execute("SELECT auth_initialized FROM school_settings WHERE id=1").fetchone()
        auth_ready = bool(auth_row and auth_row["auth_initialized"])
        admin_count = conn.execute("SELECT COUNT(*) AS c FROM users WHERE role='Admin' AND active=1").fetchone()["c"]
        if admin_count == 0 and not auth_ready:
            # New installations start with the non-Administrator login page disabled.
            conn.execute("UPDATE school_settings SET auth_required=0 WHERE id=1")
            # Preserve FK-backed sample data by using a single disabled system identity.
            system = conn.execute("SELECT id FROM users WHERE role='System' LIMIT 1").fetchone()
            if not system:
                conn.execute("INSERT INTO users(full_name,username,password_hash,role,active) VALUES(?,?,?,?,0)",
                             ("Portal System", "__system__", generate_password_hash(uuid.uuid4().hex), SYSTEM_ROLE))
                system_id = conn.execute("SELECT id FROM users WHERE username='__system__'").fetchone()["id"]
            else:
                system_id = system["id"]
            # Repoint legacy references to system account, then remove legacy accounts.
            for table, col in [("payments","recorded_by"),("audit_log","actor_id"),("exam_batches","submitted_by"),("exam_results","submitted_by"),("assignments","posted_by")]:
                try:
                    conn.execute(f"UPDATE {table} SET {col}=? WHERE {col} IS NOT NULL", (system_id,))
                except sqlite3.OperationalError:
                    pass
            conn.execute("DELETE FROM users WHERE role!='System'")
            conn.execute("UPDATE school_settings SET auth_initialized=0 WHERE id=1")
        elif admin_count > 0:
            conn.execute("UPDATE school_settings SET auth_initialized=1 WHERE id=1")

        # Repair an earlier authentication migration that accidentally left
        # payments/audit_log foreign keys pointing at users_old_auth. Preserve data.
        def repair_user_foreign_keys(table: str, create_sql: str, columns: list[str]) -> None:
            tbl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            sql_text = (tbl[0] or "") if tbl else ""
            if "users_old_auth" not in sql_text:
                return
            conn.execute("PRAGMA foreign_keys=OFF")
            tmp = f"{table}_userfk_fix"
            conn.execute(f"DROP TABLE IF EXISTS {tmp}")
            conn.execute(create_sql.format(tmp=tmp))
            conn.execute(f"INSERT INTO {tmp} ({','.join(columns)}) SELECT {','.join(columns)} FROM {table}")
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")
            conn.execute("PRAGMA foreign_keys=ON")

        repair_user_foreign_keys(
            "payments",
            """CREATE TABLE {tmp} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                method TEXT NOT NULL,
                reference_no TEXT,
                recorded_by INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'Posted' CHECK(status IN ('Posted','Reversed')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reversed_at TEXT,
                receipt_path TEXT,
                FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
                FOREIGN KEY(recorded_by) REFERENCES users(id) ON DELETE CASCADE
            )""",
            ["id","student_id","amount","method","reference_no","recorded_by","status","created_at","reversed_at","receipt_path"],
        )
        repair_user_foreign_keys(
            "audit_log",
            """CREATE TABLE {tmp} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER,
                actor_name TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(actor_id) REFERENCES users(id) ON DELETE SET NULL
            )""",
            ["id","actor_id","actor_name","action","details","created_at"],
        )


        # Remove stale SQLite triggers/objects left by older authentication migrations.
        # Some older builds created references to temporary users_roles_legacy/users_old_auth tables.
        stale_rows = conn.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
        for obj_type, obj_name, obj_sql in stale_rows:
            sql_text = (obj_sql or "")
            if "users_roles_legacy" in sql_text or "users_old_auth" in sql_text or "users_legacy" in sql_text:
                if obj_type == "trigger":
                    conn.execute(f"DROP TRIGGER IF EXISTS [{obj_name}]")

        # Provision the production Administrator from environment variables when supplied.
        # This keeps credentials out of source control and makes Render deployment deterministic.
        env_admin_username = os.environ.get("ADMIN_USERNAME", "").strip()
        env_admin_password = os.environ.get("ADMIN_PASSWORD", "")
        env_admin_name = os.environ.get("ADMIN_NAME", "").strip()
        if env_admin_username and env_admin_password and env_admin_name:
            existing = conn.execute(
                "SELECT id, role FROM users WHERE username=? LIMIT 1",
                (env_admin_username,),
            ).fetchone()
            password_hash = generate_password_hash(env_admin_password)
            if existing:
                if existing["role"] != "Admin":
                    raise RuntimeError(
                        f"ADMIN_USERNAME '{env_admin_username}' already belongs to a non-Administrator account."
                    )
                conn.execute(
                    "UPDATE users SET full_name=?, password_hash=?, active=1 WHERE id=?",
                    (env_admin_name, password_hash, existing["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO users(full_name, username, password_hash, role, active) VALUES(?,?,?,?,1)",
                    (env_admin_name, env_admin_username, password_hash, "Admin"),
                )
            conn.execute("UPDATE school_settings SET auth_initialized=1, auth_required=1 WHERE id=1")

        # Force SQLite to materialize/validate the final schema after migration cleanup.
        conn.execute("PRAGMA foreign_key_check")
        conn.commit()


def q(sql: str, params: Iterable[Any] = (), one: bool = False):
    cur = get_db().execute(sql, tuple(params))
    rows = cur.fetchall()
    cur.close()
    if one:
        return rows[0] if rows else None
    return rows


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    db = get_db()
    cur = db.execute(sql, tuple(params))
    db.commit()
    return cur.lastrowid


def school_settings():
    """Return the single school-wide portal settings row, creating it when needed."""
    row = q("SELECT * FROM school_settings WHERE id = 1", one=True)
    if row:
        return row
    execute(
        """
        INSERT INTO school_settings(
            id, school_name, admission_prefix, admission_suffix,
            student_name_prefix, student_name_suffix, currency_code, school_fee,
            auth_required, prelogin_sections, developer_name, company_name
        ) VALUES (1, 'School Portal System', 'ADM-', '', '', '', 'KES', 0, 0, 'institution,history,achievements,owners,developer,company', 'Toror Technology and Innovations Ltd.', 'Toror Technology and Innovations Ltd.')
        """
    )
    return q("SELECT * FROM school_settings WHERE id = 1", one=True)


def audit(actor_id: int | None, actor_name: str, action: str, details: str) -> None:
    execute(
        "INSERT INTO audit_log(actor_id, actor_name, action, details) VALUES (?, ?, ?, ?)",
        (actor_id, actor_name, action, details),
    )


# -------------------------
# Local role session helpers
# -------------------------
def _auth_serializer():
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="school-auth-v3")


def _auth_token_for(user_id: int) -> str:
    return _auth_serializer().dumps({"uid": int(user_id)})


def _user_from_auth_token(token: str):
    try:
        data = _auth_serializer().loads(token, max_age=_AUTH_COOKIE_MAX_AGE)
        uid = int(data.get("uid", 0))
    except (BadSignature, SignatureExpired, ValueError, TypeError, AttributeError):
        return None
    if not uid:
        return None
    return q("SELECT id, full_name, username, role, student_id, active FROM users WHERE id = ? AND active = 1 AND role != 'System'", (uid,), one=True)


@app.before_request
def load_current_user() -> None:
    g.user = None
    user_id = session.get("user_id")
    if user_id:
        g.user = q("SELECT id, full_name, username, role, student_id, active FROM users WHERE id = ? AND active = 1 AND role != 'System'", (user_id,), one=True)
        if g.user:
            session.permanent = True
            return
    # Recover authentication if the Flask session cookie was lost/rejected.
    token = request.cookies.get(_AUTH_COOKIE, "")
    if token:
        recovered = _user_from_auth_token(token)
        if recovered:
            g.user = recovered
            session.permanent = True
            session["user_id"] = recovered["id"]


@app.after_request
def persist_auth_cookie(response):
    user = getattr(g, "user", None)
    if user:
        response.set_cookie(
            _AUTH_COOKIE,
            _auth_token_for(user["id"]),
            max_age=_AUTH_COOKIE_MAX_AGE,
            httponly=True,
            secure=app.config.get("SESSION_COOKIE_SECURE", False),
            samesite="Lax",
            path="/",
        )
    return response


def current_user():
    return getattr(g, "user", None)


def login_required(view: Callable):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login", role=request.args.get("role", "")))
        return view(*args, **kwargs)
    return wrapper


def role_required(*roles: str):
    def decorator(view: Callable):
        @wraps(view)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user or user["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return decorator


def auth_initialized() -> bool:
    row = q("SELECT auth_initialized FROM school_settings WHERE id=1", one=True)
    return bool(row and row["auth_initialized"])


def auth_required() -> bool:
    row = q("SELECT auth_required FROM school_settings WHERE id=1", one=True)
    return bool(row and row["auth_required"])


def role_target(role: str) -> str:
    return {
        "Admin": url_for("admin_dashboard"),
        "ICT": url_for("ict_dashboard"),
        "Finance": url_for("finance_dashboard"),
        "Teacher": url_for("teacher_dashboard"),
        "Student": url_for("student_dashboard"),
        "Parent": url_for("parent_dashboard"),
        "Librarian": url_for("librarian_dashboard"),
    }[role]


def enter_role_without_login(role: str):
    if role == "Parent" and not parent_portal_enabled():
        flash("Parent accounts are disabled for this institution mode.", "warning")
        return redirect(url_for("index"))
    # Passwordless mode is intended only as a controlled demo/single-user setup.
    # Once a role has multiple active accounts, require an actual identity so one
    # user's dashboard cannot silently become another user's account.
    if role == "Admin" or auth_required():
        return redirect(url_for("login", role=role))
    users = q(
        "SELECT id FROM users WHERE role=? AND active=1 AND role!='System' ORDER BY id",
        (role,),
    )
    if not users:
        flash(f"No active {role} account has been created yet.", "warning")
        return redirect(url_for("index"))
    if len(users) != 1:
        flash(f"{role} access requires login because multiple active {role} accounts exist.", "warning")
        return redirect(url_for("login", role=role))
    session.clear()
    session["user_id"] = users[0]["id"]
    return redirect(role_target(role))


def selected_role_from_request(default=""):
    role = (request.args.get("role") or request.form.get("role") or default).strip()
    return role if role in ALL_PORTAL_ROLES else default




def theme_style(settings=None) -> str:
    settings = settings or school_settings()
    def esc(v):
        return str(v).replace('<','').replace('>','').replace('"','').replace(';','')
    bg=esc(settings['background_color']); panel=esc(settings['panel_color']); text=esc(settings['text_color']); muted=esc(settings['muted_text_color'])
    if settings['theme_mode']=='light':
        bg='#f5f7fb'; panel='#ffffff'; text='#18212f'; muted='#667085'
    css=(
        f":root{{--bg:{bg};--panel:{panel};"
        f"--primary-blue:{esc(settings['primary_color'])};--deep-accent-blue:{esc(settings['accent_color'])};"
        f"--primary-text:{text};--muted-text:{muted};"
        f"--font:'{esc(settings['font_family'])}',Inter,system-ui,sans-serif;--heading-font:'{esc(settings['heading_font'])}',{esc(settings['font_family'])},Inter,sans-serif;--radius:{int(settings['radius_px'] or 12)}px;"
        f"--radius-sm:{int(settings['button_radius_px'] or 10)}px;--sidebar-bg:{esc(settings['sidebar_color'])};"
        f"--header-bg:{esc(settings['header_color'])};}}"
    )
    extra=settings['custom_css'] or ''
    # Admin-entered CSS is deliberately limited to a length bound and unsafe import/javascript patterns.
    if len(extra)>12000 or re.search(r'@import|javascript:|expression\s*\(', extra, re.I):
        extra=''
    return css+extra


def current_landing_url() -> str:
    return url_for('index', _external=True)

def portal_qr_data_uri() -> str:
    payload=url_for("index", _external=True)
    qr=qrcode.QRCode(version=2,box_size=5,border=2); qr.add_data(payload); qr.make(fit=True)
    buf=io.BytesIO(); qr.make_image().save(buf,format="PNG"); return "data:image/png;base64,"+base64.b64encode(buf.getvalue()).decode("ascii")

@app.context_processor
def auth_template_context():
    settings=school_settings()
    return {
        "current_user": current_user(), "school_settings": settings, "portal_title": settings["school_name"], "theme_color": settings["primary_color"], "all_roles": ALL_PORTAL_ROLES, "public_roles": PUBLIC_ROLES,
        "theme_style": theme_style(settings), "portal_landing_url": current_landing_url(),
        "institution_type": settings['institution_type'], "learner_label": settings['learner_label'],
        "learner_plural": "Pupils" if settings['learner_label'].lower().startswith('pupil') else "Students",
        "staff_label": settings['staff_label'],
        "staff_plural": "Teachers / Lecturers" if 'Lecturer' in (settings['staff_label'] or '') else "Employees",
        "help_enabled": True,
        "parent_portal_enabled": parent_portal_enabled(),
        "portal_qr_data_uri": portal_qr_data_uri(),
    }


def workspace_for(role: str) -> str:
    return {
        "Admin": "Admin Command Centre",
        "ICT": "ICT Control Panel",
        "Finance": "Finance Workspace",
        "Teacher": "Teacher Dashboard",
        "Student": "Student Dashboard",
        "Parent": "Parent Dashboard",
        "Librarian": "Library Workspace",
        "Staff": "Staff Workspace",
    }.get(role, "School Portal System")


def navigation_items(role: str, settings):
    order=[x.strip() for x in (settings["menu_order"] or "").split(",") if x.strip()]
    labels={
        "Home": settings["home_label"],
        "Assignments": settings["assignments_label"],
        "Results": settings["results_label"],
        "Messages": settings["messages_label"],
        "Finance": settings["finance_label"],
        "Branding": settings["branding_label"],
        "Submissions": "Submissions",
        "Online classes": "Online classes",
        "My children": "My children",
        "Results & fees": "Results & fees",
        "Teacher communication": "Teacher communication",
        "Payments": "Payments",
        "Elections": "Elections",
        "Library": "Library",
        "Institution": "About the institution",
        "Theme": "Theme",
        "Navigation order": "Navigation order",
        "Members": "Members",
    }
    allowed={
        "Teacher": ["Home","Assignments","Submissions","Online classes","Elections","Library","Institution"],
        "Student": ["Home","Assignments","Results","Online classes","Elections","Library","Institution"],
        "Parent": ["Home","My children","Results & fees","Teacher communication","Library","Institution"],
        "Finance": ["Home","Finance","Payments","Library","Institution"],
        "ICT": ["Home","Branding","Theme","Navigation order","Elections","Library","Institution","Members"],
        "Librarian": ["Home","Library","Institution"],
        "Admin": ["Home","Finance","Elections","Library","Institution","Members"],
    }.get(role, ["Home","Institution"])
    if not int(settings["elections_enabled"] or 0) and role not in {"Admin","ICT"}:
        allowed=[x for x in allowed if x!="Elections"]
    if not int(settings["library_enabled"] or 0) and role not in {"Admin","ICT","Librarian"}:
        allowed=[x for x in allowed if x!="Library"]
    anchor_map={"Home":"home","Assignments":"assignments","Submissions":"submissions","Online classes":"classes","Results":"results","My children":"children","Results & fees":"results","Teacher communication":"messages","Finance":"finance","Payments":"payments","Branding":"branding","Theme":"theme","Navigation order":"navigation","Elections":"elections","Library":"library","Institution":"institution","Members":"users"}
    result=[]
    for key in order:
        if key in allowed and key not in [r["key"] for r in result]:
            result.append({"key":key,"label":labels.get(key,key),"anchor":anchor_map[key]})
    for key in allowed:
        if key not in [r["key"] for r in result]:
            result.append({"key":key,"label":labels.get(key,key),"anchor":anchor_map[key]})
    return result


def portal_student(student_id=None):
    user=current_user()
    if user and user["role"] in {"Student","Parent"}:
        linked_id=user["student_id"]
        if not linked_id:
            return None
        # Never allow a URL/query parameter to switch a Student/Parent to another pupil.
        return q("SELECT * FROM students WHERE id=? AND active=1", (linked_id,), one=True)
    if student_id:
        return q("SELECT * FROM students WHERE id=? AND active=1", (student_id,), one=True)
    return q("SELECT * FROM students WHERE active=1 ORDER BY id LIMIT 1", one=True)


def parent_portal_enabled() -> bool:
    row = q("SELECT parent_portal_enabled FROM school_settings WHERE id=1", one=True)
    if row is not None:
        return bool(row["parent_portal_enabled"])
    return school_settings()["institution_type"] in {"Primary School", "Secondary School", "Mixed Institution"}

def teacher_or_admin() -> bool:
    return bool(current_user() and current_user()["role"] in {"Teacher", "Admin"})

def parent_children(user):
    """Return children the authenticated Parent may legitimately switch to."""
    if not user or user["role"] != "Parent" or not user["student_id"]:
        return []
    linked = q("SELECT * FROM students WHERE id=? AND active=1", (user["student_id"],), one=True)
    if not linked:
        return []
    # Prefer stable guardian contact fields; names are a fallback for older records.
    filters=[]; params=[]
    for column in ("guardian_phone", "guardian_email", "alt_guardian_phone", "alt_guardian_email"):
        value=linked[column]
        if value:
            filters.append(f"{column}=?"); params.append(value)
    if not filters and linked["guardian_name"]:
        filters.append("guardian_name=?"); params.append(linked["guardian_name"])
    if not filters:
        return [linked]
    sql="SELECT * FROM students WHERE active=1 AND (" + " OR ".join(filters) + ") ORDER BY full_name"
    children=q(sql, params)
    # Always include the explicitly linked child even if legacy contact data is incomplete.
    if not any(row["id"] == linked["id"] for row in children):
        children=[linked, *children]
    return children


def can_access_student(student_id: int, *, write: bool=False) -> bool:
    user=current_user()
    if not user:
        return False
    role=user["role"]
    if role in {"Admin", "ICT", "Finance", "Librarian"}:
        return not write or role in {"Admin", "ICT"}
    if role == "Student":
        return bool(user["student_id"] == student_id and not write)
    if role == "Parent":
        return any(row["id"] == student_id for row in parent_children(user)) and not write
    return False


def assignment_rows(grade=None):
    if grade:
        return q("""SELECT a.*, u.full_name AS teacher_name, COUNT(s.id) AS submissions
                   FROM assignments a LEFT JOIN users u ON u.id=a.posted_by
                   LEFT JOIN submissions s ON s.assignment_id=a.id
                   WHERE a.grade=? GROUP BY a.id ORDER BY a.created_at DESC, a.id DESC""", (grade,))
    return q("""SELECT a.*, u.full_name AS teacher_name, COUNT(s.id) AS submissions
               FROM assignments a LEFT JOIN users u ON u.id=a.posted_by
               LEFT JOIN submissions s ON s.assignment_id=a.id
               GROUP BY a.id ORDER BY a.created_at DESC, a.id DESC""")


def make_qr_token(document_type: str, student_id: int) -> str:
    return f"{document_type.lower().replace(' ', '-')}-{student_id}-{uuid.uuid4().hex}"

def create_qr_file(token: str) -> Path:
    qr=qrcode.QRCode(version=1, box_size=8, border=3)
    qr.add_data(url_for("verify_document", token=token, _external=True)); qr.make(fit=True)
    path=QR_DIR/f"{token}.png"; qr.make_image().save(path); return path

def result_release_info(student_id: int):
    settings=school_settings(); limit=float(settings["result_download_balance_limit"] or 0)
    student=q("SELECT balance FROM students WHERE id=?",(student_id,),one=True)
    balance=float(student["balance"] or 0) if student else 0
    batches=q("SELECT b.*, COUNT(r.id) AS entries FROM exam_batches b JOIN exam_results r ON r.batch_id=b.id WHERE r.student_id=? GROUP BY b.id ORDER BY b.created_at DESC,b.id DESC",(student_id,))
    return [{"batch":b,"eligible":b["finance_status"]=="Approved" and balance<=limit,"limit":limit,"balance":balance} for b in batches]

def generate_result_pdf(student,batch,results,doc,qr_path):
    buf=io.BytesIO(); c=canvas.Canvas(buf,pagesize=A4); w,h=A4; settings=school_settings()
    c.setTitle(f"Result - {student['full_name']}"); c.setFont("Helvetica-Bold",16); c.drawString(44,h-52,settings["school_name"])
    c.setFont("Helvetica",10); c.drawString(44,h-70,"Official Student Result")
    c.drawString(44,h-94,f"Student: {student['full_name']}"); c.drawString(44,h-110,f"Admission No: {student['admission_no']}   Grade: {student['grade']}"); c.drawString(44,h-126,f"Term: {batch['term']}   Finance status: {batch['finance_status']}")
    y=h-160; c.setFont("Helvetica-Bold",10); c.drawString(44,y,"Subject"); c.drawString(250,y,"Mark"); c.drawString(330,y,"Out of"); y-=18; c.setFont("Helvetica",10)
    for r in results:
        c.drawString(44,y,str(r['subject'])); c.drawString(250,y,f"{r['mark']:.1f}"); c.drawString(330,y,f"{r['max_mark']:.1f}"); y-=16
        if y<120: c.showPage(); y=h-60
    c.drawImage(str(qr_path),w-145,60,width=90,height=90,preserveAspectRatio=True,mask='auto'); c.setFont("Helvetica",7); c.drawString(w-148,50,"Scan to verify authenticity"); c.drawString(44,44,f"Verification token: {doc['qr_token']}"); c.drawString(44,32,"Issued by School Portal System"); c.save(); buf.seek(0); return buf

def embed_qr_in_document(source_path: Path, qr_path: Path, token: str) -> str:
    """Create a verified copy with the QR visible on the document when possible."""
    suffix=source_path.suffix.lower()
    if suffix in {".png",".jpg",".jpeg",".webp"}:
        img=Image.open(source_path).convert("RGB")
        qr=Image.open(qr_path).convert("RGB").resize((220,220))
        pad=20; canvas_img=Image.new("RGB",(img.width,max(img.height,260)),"white"); canvas_img.paste(img,(0,0));
        x=max(0,canvas_img.width-qr.width-pad); canvas_img.paste(qr,(x,20)); out=DOC_DIR/f"verified-{token}.jpg"; canvas_img.save(out,quality=92); return "uploads/documents/"+out.name
    if suffix==".pdf":
        reader=PdfReader(str(source_path)); writer=PdfWriter()
        overlay=io.BytesIO(); c=canvas.Canvas(overlay,pagesize=A4); w,h=A4; c.drawImage(str(qr_path),w-150,h-150,width=90,height=90,mask='auto'); c.setFont("Helvetica",7); c.drawString(w-154,h-158,"Scan to verify authenticity"); c.save(); overlay.seek(0); ov=PdfReader(overlay).pages[0]
        for i,page in enumerate(reader.pages):
            if i==0: page.merge_page(ov)
            writer.add_page(page)
        out=DOC_DIR/f"verified-{token}.pdf"; writer.write(str(out)); return "uploads/documents/"+out.name
    return ""

def generate_exam_card_pdf(student,batch_name,doc,qr_path):
    buf=io.BytesIO(); c=canvas.Canvas(buf,pagesize=A4); w,h=A4; settings=school_settings(); c.setTitle(f"Exam Card - {student['full_name']}")
    c.setFont("Helvetica-Bold",18); c.drawString(44,h-58,settings["school_name"]); c.setFont("Helvetica-Bold",14); c.drawString(44,h-84,"EXAMINATION CARD")
    c.setFont("Helvetica",11); y=h-130
    for line in [f"Student: {student['full_name']}",f"Admission No: {student['admission_no']}",f"Grade: {student['grade']}",f"Examination: {batch_name}",f"Fee balance: {settings['currency_code']} {float(student['balance'] or 0):,.0f}"]:
        c.drawString(60,y,line); y-=22
    c.drawString(60,y-8,"Present this card when requested by the school."); c.drawImage(str(qr_path),w-170,95,width=100,height=100,preserveAspectRatio=True,mask='auto'); c.setFont("Helvetica",7); c.drawString(w-174,84,"Scan to verify authenticity"); c.drawString(44,44,f"Verification token: {doc['qr_token']}"); c.save(); buf.seek(0); return buf

# -------------------------
# Context / templates
# -------------------------
@app.context_processor
def inject_globals():
    settings = school_settings()
    return {
        "now_year": datetime.now().year,
        "current_user": current_user(),
        "workspace_for": workspace_for,
        "school_settings": settings,
        "admin_login_path": ADMIN_LOGIN_PATH,
        "public_roles": PUBLIC_ROLES,
        "hidden_roles": HIDDEN_ROLES,
        "theme_color": settings["background_color"],
        "theme_accent": settings["primary_color"],
        "elections_enabled": bool(settings["elections_enabled"]),
        "library_enabled": bool(settings["library_enabled"]),
        "institution_types": INSTITUTION_TYPES,
    }


# -------------------------
# Routes
# -------------------------
@app.route("/offline")
def offline():
    settings = school_settings()
    return render_template("offline.html", portal_title=settings["school_name"], school_settings=settings, theme_color=settings["primary_color"])


@app.route("/pulse_receiver", methods=["POST", "GET"])
def pulse_receiver():
    payload = request.get_json(silent=True) or {}
    request_id = str(payload.get("request_id") or request.headers.get("X-Pulse-ID") or uuid.uuid4().hex)
    source = str(payload.get("source") or payload.get("sender") or request.headers.get("X-Pulse-Source") or "unknown")[:200]
    reply_payload = {
        "type": "school_portal_pulse",
        "event": "received",
        "request_id": request_id,
        "received_from": source,
        "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "reply": True,
    }
    requested_reply = str(payload.get("reply_to") or "").strip()
    configured_peer_host = (urllib.parse.urlparse(PULSE_PEER_URL).hostname or "").lower()
    allowed_hosts = set(PULSE_ALLOWED_CALLBACK_HOSTS)
    if configured_peer_host:
        allowed_hosts.add(configured_peer_host)
    reply_url = PULSE_PEER_URL
    if requested_reply:
        parsed_reply = urllib.parse.urlparse(requested_reply)
        if parsed_reply.scheme == "https" and parsed_reply.netloc and (parsed_reply.hostname or "").lower() in allowed_hosts:
            reply_url = requested_reply.rstrip("/")

    def post_json(target: str) -> tuple[bool, int]:
        parsed = urllib.parse.urlparse(target)
        if parsed.scheme != "https" or not parsed.netloc:
            return False, 0
        body = json.dumps(reply_payload, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(target, data=body, method="POST", headers={"Content-Type":"application/json","Accept":"application/json","User-Agent":"SchoolPortal-Pulse/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=PULSE_TIMEOUT_SECONDS) as response:
                return True, int(getattr(response, "status", 200))
        except urllib.error.HTTPError as exc:
            return False, int(exc.code)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return False, 0

    def reply_worker():
        ok, status = post_json(reply_url)
        # The configured peer was supplied as a bare origin in the request.
        # If that origin does not expose POST, retry its conventional pulse route.
        parsed = urllib.parse.urlparse(reply_url)
        if not ok and status == 404 and parsed.path in ("", "/"):
            post_json(reply_url.rstrip("/") + "/pulse_receiver")
    threading.Thread(target=reply_worker, daemon=True, name="pulse-peer-reply").start()
    threading.Thread(target=reply_worker, daemon=True, name="pulse-peer-reply").start()
    return jsonify({"ok": True, "received": True, "reply_scheduled": True, "request_id": request_id, "service": "school-portal", "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z"}), 200


@app.route("/pulse", methods=["POST", "GET"])
def pulse():
    return pulse_receiver()


@app.route("/health")
def health():
    db_ok = True
    try:
        q("SELECT 1", one=True)
    except Exception:
        db_ok = False
    return jsonify({"ok": db_ok, "database": "ok" if db_ok else "error", "persistent_storage": PERSISTENT_STORAGE, "pulse_peer": PULSE_PEER_URL, "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z"}), (200 if db_ok else 503)


@app.route("/")
def index():
    settings = school_settings()
    if current_user():
        user = current_user()
        target = {"Admin":"admin_dashboard","ICT":"ict_dashboard","Finance":"finance_dashboard","Teacher":"teacher_dashboard","Student":"student_dashboard","Parent":"parent_dashboard","Librarian":"librarian_dashboard"}.get(user["role"], "login")
        return redirect(url_for(target))
    return render_template("login.html", portal_title=settings["school_name"], school_settings=settings, theme_color=settings["primary_color"], setup_required=not auth_initialized())


@app.route("/login", methods=["GET", "POST"])
def login():
    settings = school_settings()
    role = selected_role_from_request()
    if role == "Parent" and not parent_portal_enabled():
        flash("Parent / Guardian portal is disabled for this institution mode.", "warning")
        return redirect(url_for("index"))
    # Keep the legacy single-account/passwordless portal shortcut on GET, but
    # NEVER bypass an explicit credential POST. Accounts created by Admin/ICT
    # must always be able to authenticate with their own username/password.
    if request.method == "GET" and role and role != "Admin" and not auth_required():
        return enter_role_without_login(role)
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        role = selected_role_from_request().strip()
        user = q("SELECT * FROM users WHERE lower(username)=? AND active=1 AND role=?", (username, role), one=True)
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Invalid username, password, or role.", "danger")
            return render_template("login.html", portal_title=settings["school_name"], school_settings=settings, theme_color=settings["primary_color"], login_role=role, error="Invalid username, password, or role.", setup_required=not auth_initialized())
        session.clear()
        session.permanent = True
        session["user_id"] = user["id"]
        return redirect({"Admin":url_for("admin_dashboard"),"ICT":url_for("ict_dashboard"),"Finance":url_for("finance_dashboard"),"Teacher":url_for("teacher_dashboard"),"Student":url_for("student_dashboard"),"Parent":url_for("parent_dashboard"),"Librarian":url_for("librarian_dashboard")}[user["role"]])
    return render_template("login.html", portal_title=settings["school_name"], school_settings=settings, theme_color=settings["primary_color"], login_role=role, setup_required=not auth_initialized())


@app.route("/register", methods=["GET", "POST"])
def register_admin():
    if auth_initialized():
        return redirect(url_for("login"))
    settings = school_settings()
    if request.method == "POST":
        full_name=request.form.get("full_name","").strip(); username=request.form.get("username","").strip(); password=request.form.get("password","")
        confirm=request.form.get("confirm_password","")
        if not full_name or not username or len(password) < 4 or password != confirm:
            flash("Enter a name, unique username, and matching password of at least 4 characters.", "danger")
            return render_template("register.html", school_settings=settings, portal_title=settings["school_name"])
        try:
            execute("INSERT INTO users(full_name,username,password_hash,role,active) VALUES(?,?,?,?,1)", (full_name,username,generate_password_hash(password),"Admin"))
            execute("UPDATE school_settings SET auth_initialized=1 WHERE id=1")
        except sqlite3.IntegrityError:
            flash("That username is already in use.", "danger")
            return render_template("register.html", school_settings=settings, portal_title=settings["school_name"])
        flash("Administrator account created. Please log in.", "success")
        return redirect(url_for("login", role="Admin"))
    return render_template("register.html", school_settings=settings, portal_title=settings["school_name"])


def enter_role(role: str):
    if role not in ALL_PORTAL_ROLES:
        abort(404)
    return enter_role_without_login(role)


@app.route("/admin")
def admin_entry():
    abort(404)

@app.route(ADMIN_LOGIN_PATH)
def admin_hidden_entry():
    return enter_role("Admin")
@app.route("/ict")
def ict_entry(): return enter_role("ICT")
@app.route("/finance")
def finance_entry(): return enter_role("Finance")
@app.route("/teacher")
def teacher_entry(): return enter_role("Teacher")
@app.route("/student")
def student_entry(): return enter_role("Student")
@app.route("/parent")
def parent_entry(): return enter_role("Parent")
@app.route("/librarian")
def librarian_entry(): return enter_role("Librarian")


@app.route("/coming-soon/<feature>")
def coming_soon(feature: str):
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    response = redirect(url_for("index"))
    response.delete_cookie(_AUTH_COOKIE, path="/")
    return response


@app.route("/favicon.ico")
def favicon():
    return send_file(BASE_DIR / "static" / "icons" / "favicon.ico", mimetype="image/vnd.microsoft.icon", conditional=True, max_age=86400)


@app.route("/sw.js")
def service_worker():
    return send_file(BASE_DIR / "static" / "sw.js", mimetype="application/javascript", conditional=True, max_age=0)


@app.route("/dashboard")
@login_required
@role_required("Teacher")
def dashboard():
    user = current_user()
    settings = school_settings()
    selected_grade = request.args.get("grade", "").strip() or None
    grade_params = (selected_grade,) if selected_grade else ()
    student_where = "WHERE grade = ?" if selected_grade else ""
    payment_where = "WHERE s.grade = ?" if selected_grade else ""

    students = q(f"SELECT * FROM students {student_where} ORDER BY created_at DESC, id DESC LIMIT 24", grade_params)
    available_students = q("SELECT * FROM students WHERE active = 1 ORDER BY created_at DESC, id DESC LIMIT 24")
    payments = q(
        f"""
        SELECT p.*, s.full_name AS student_name, s.admission_no, u.full_name AS recorded_by_name
        FROM payments p
        JOIN students s ON s.id = p.student_id
        JOIN users u ON u.id = p.recorded_by
        {payment_where}
        ORDER BY p.created_at DESC, p.id DESC
        LIMIT 24
        """,
        grade_params,
    )
    audits = q("SELECT * FROM audit_log ORDER BY created_at DESC, id DESC LIMIT 12")
    available_grades = [row["grade"] for row in q("SELECT DISTINCT grade FROM students ORDER BY grade")]

    summary_students = q(f"SELECT COUNT(*) AS c FROM students {student_where}", grade_params, one=True)["c"]
    summary_active = q("SELECT COUNT(*) AS c FROM students WHERE grade = ? AND active = 1" if selected_grade else "SELECT COUNT(*) AS c FROM students WHERE active = 1", grade_params if selected_grade else (), one=True)["c"]
    summary_paid = q("SELECT COUNT(*) AS c FROM students WHERE grade = ? AND payment_status = 'Paid'" if selected_grade else "SELECT COUNT(*) AS c FROM students WHERE payment_status = 'Paid'", grade_params if selected_grade else (), one=True)["c"]
    summary_pending = q("SELECT COUNT(*) AS c FROM students WHERE grade = ? AND payment_status = 'Pending'" if selected_grade else "SELECT COUNT(*) AS c FROM students WHERE payment_status = 'Pending'", grade_params if selected_grade else (), one=True)["c"]
    summary_collections = q("SELECT COALESCE(SUM(amount), 0) AS total FROM payments p JOIN students s ON s.id = p.student_id WHERE p.status = 'Posted' AND s.grade = ?" if selected_grade else "SELECT COALESCE(SUM(amount), 0) AS total FROM payments WHERE status = 'Posted'", grade_params if selected_grade else (), one=True)["total"]
    total_balance = q("SELECT COALESCE(SUM(balance), 0) AS total FROM students WHERE active = 1", one=True)["total"]

    summary = {
        "students": summary_students,
        "active_students": summary_active,
        "collections": summary_collections,
        "paid": summary_paid,
        "pending": summary_pending,
        "balance": total_balance,
    }

    exam_grade = request.args.get("exam_grade", "").strip() or selected_grade or (available_grades[0] if available_grades else "")
    exam_term = request.args.get("exam_term", "").strip() or "Term 1"
    exam_students = q(
        "SELECT id, full_name, admission_no, grade FROM students WHERE active = 1 AND grade = ? ORDER BY full_name",
        (exam_grade,),
    ) if exam_grade else []
    exam_subjects = [row["subject"] for row in q("SELECT DISTINCT subject FROM exam_results WHERE grade = ? ORDER BY subject", (exam_grade,))] if exam_grade else []
    if not exam_subjects:
        exam_subjects = ["Math", "English", "Science"]

    selected_student_id = request.args.get("student_id", type=int)
    selected_student = None
    student_payments = []
    if selected_student_id:
        selected_student = q("SELECT * FROM students WHERE id = ?", (selected_student_id,), one=True)
        if selected_grade and selected_student and selected_student["grade"] != selected_grade:
            selected_student = None
        if selected_student:
            student_payments = q(
                """
                SELECT p.*, u.full_name AS recorded_by_name
                FROM payments p
                JOIN users u ON u.id = p.recorded_by
                WHERE p.student_id = ?
                ORDER BY p.created_at DESC, p.id DESC
                """,
                (selected_student_id,),
            )
    if not selected_student and students:
        selected_student = students[0]
        student_payments = q(
            """
            SELECT p.*, u.full_name AS recorded_by_name
            FROM payments p
            JOIN users u ON u.id = p.recorded_by
            WHERE p.student_id = ?
            ORDER BY p.created_at DESC, p.id DESC
            """,
            (students[0]["id"],),
        )

    exam_batches = q(
        """
        SELECT b.*, u.full_name AS submitted_by_name, COUNT(r.id) AS entries, ROUND(AVG(r.mark), 1) AS mean_mark
        FROM exam_batches b
        LEFT JOIN exam_results r ON r.batch_id = b.id
        JOIN users u ON u.id = b.submitted_by
        WHERE b.grade = ?
        GROUP BY b.id
        ORDER BY b.created_at DESC, b.id DESC
        LIMIT 8
        """,
        (exam_grade,),
    ) if exam_grade else []
    exam_summary = q(
        """
        SELECT grade,
               COUNT(DISTINCT student_id) AS pupils,
               COUNT(DISTINCT subject) AS subjects,
               COUNT(DISTINCT batch_id) AS batches,
               ROUND(AVG(mark), 1) AS mean_score,
               ROUND(MAX(mark), 1) AS highest_score,
               ROUND(MIN(mark), 1) AS lowest_score
        FROM exam_results
        GROUP BY grade
        ORDER BY grade
        """
    )
    exam_subject_summary = q(
        """
        SELECT subject,
               COUNT(*) AS entries,
               ROUND(AVG(mark), 1) AS mean_score,
               ROUND(MAX(mark), 1) AS highest_score,
               ROUND(MIN(mark), 1) AS lowest_score
        FROM exam_results
        GROUP BY subject
        ORDER BY mean_score DESC, subject
        """
    )

    return render_template(
        "dashboard.html",
        role=user["role"],
        workspace=workspace_for(user["role"]),
        summary=summary,
        students=students,
        payments=payments,
        audits=audits,
        selected_student=selected_student,
        selected_student_payments=student_payments,
        selected_grade=selected_grade,
        available_grades=available_grades,
        available_students=available_students,
        settings=settings,
        exam_grade=exam_grade,
        exam_term=exam_term,
        exam_students=exam_students,
        exam_subjects=exam_subjects,
        exam_batches=exam_batches,
        exam_summary=exam_summary,
        exam_subject_summary=exam_subject_summary,
        user_students=q("SELECT id, full_name, admission_no FROM students ORDER BY full_name"),
    )


@app.route("/admin-dashboard")
@login_required
@role_required("Admin")
def admin_dashboard():
    settings = school_settings()
    students = q("SELECT * FROM students ORDER BY created_at DESC, id DESC LIMIT 20")
    payments = q(
        """
        SELECT p.*, s.full_name AS student_name, s.admission_no, u.full_name AS recorded_by_name
        FROM payments p
        JOIN students s ON s.id = p.student_id
        JOIN users u ON u.id = p.recorded_by
        ORDER BY p.created_at DESC, p.id DESC
        LIMIT 20
        """
    )
    users = q("""SELECT u.*, COALESCE(u.title, '') AS title, COALESCE(u.department, '') AS department,
                      CASE WHEN u.active=1 THEN 'Active' ELSE 'Archived' END AS access_state
               FROM users u ORDER BY u.active DESC, u.created_at DESC""")
    audits = q("SELECT * FROM audit_log ORDER BY created_at DESC, id DESC LIMIT 20")
    total_students = q("SELECT COUNT(*) AS c FROM students", one=True)["c"]
    active_students = q("SELECT COUNT(*) AS c FROM students WHERE active = 1", one=True)["c"]
    paid_students = q("SELECT COUNT(*) AS c FROM students WHERE payment_status = 'Paid'", one=True)["c"]
    pending_students = q("SELECT COUNT(*) AS c FROM students WHERE payment_status = 'Pending'", one=True)["c"]
    total_income = q("SELECT COALESCE(SUM(amount), 0) AS total FROM payments WHERE status = 'Posted'", one=True)["total"]
    total_balance = q("SELECT COALESCE(SUM(balance), 0) AS total FROM students", one=True)["total"]
    avg_balance = q("SELECT COALESCE(AVG(balance), 0) AS total FROM students", one=True)["total"]
    categories = {
        "students": q("SELECT grade, COUNT(*) AS c FROM students GROUP BY grade ORDER BY grade"),
        "employees": q("SELECT role, COUNT(*) AS c FROM users GROUP BY role ORDER BY role"),
        "payments": q("SELECT method, COUNT(*) AS c FROM payments WHERE status = 'Posted' GROUP BY method ORDER BY method"),
    }

    exam_grade_summary = q(
        """
        SELECT grade,
               COUNT(DISTINCT student_id) AS pupils,
               COUNT(DISTINCT subject) AS subjects,
               COUNT(DISTINCT batch_id) AS batches,
               ROUND(AVG(mark), 1) AS mean_score,
               ROUND(MAX(mark), 1) AS highest_score,
               ROUND(MIN(mark), 1) AS lowest_score
        FROM exam_results
        GROUP BY grade
        ORDER BY grade
        """
    )
    exam_term_summary = q(
        """
        SELECT grade,
               term,
               COUNT(DISTINCT student_id) AS pupils,
               COUNT(DISTINCT subject) AS subjects,
               ROUND(AVG(mark), 1) AS mean_score,
               ROUND(MAX(mark), 1) AS highest_score,
               ROUND(MIN(mark), 1) AS lowest_score
        FROM exam_results
        GROUP BY grade, term
        ORDER BY grade, term
        """
    )
    exam_subject_summary = q(
        """
        SELECT subject,
               COUNT(*) AS entries,
               ROUND(AVG(mark), 1) AS mean_score,
               ROUND(MAX(mark), 1) AS highest_score,
               ROUND(MIN(mark), 1) AS lowest_score
        FROM exam_results
        GROUP BY subject
        ORDER BY mean_score DESC, subject
        """
    )
    recent_exam_batches = q(
        """
        SELECT b.*, u.full_name AS submitted_by_name, COUNT(r.id) AS entries, ROUND(AVG(r.mark), 1) AS mean_mark
        FROM exam_batches b
        LEFT JOIN exam_results r ON r.batch_id = b.id
        JOIN users u ON u.id = b.submitted_by
        GROUP BY b.id
        ORDER BY b.created_at DESC, b.id DESC
        LIMIT 12
        """
    )
    exam_school_summary = {
        "batches": q("SELECT COUNT(*) AS c FROM exam_batches", one=True)["c"],
        "results": q("SELECT COUNT(*) AS c FROM exam_results", one=True)["c"],
        "mean": q("SELECT COALESCE(AVG(mark), 0) AS c FROM exam_results", one=True)["c"],
        "best": q("SELECT ROUND(MAX(mark), 1) AS c FROM exam_results", one=True)["c"],
    }

    elections=q("SELECT * FROM elections ORDER BY created_at DESC")
    election_candidates={e["id"]:q("SELECT * FROM election_candidates WHERE election_id=? ORDER BY position,name",(e["id"],)) for e in elections}
    library_items=q("SELECT * FROM library_items ORDER BY active DESC,category,title")

    return render_template(
        "admin_dashboard.html",
        workspace=workspace_for("Admin"),
        settings=settings,
        students=students,
        payments=payments,
        users=users,
        audits=audits,
        summary={
            "total_students": total_students,
            "active_students": active_students,
            "paid_students": paid_students,
            "pending_students": pending_students,
            "total_income": total_income,
            "total_balance": total_balance,
            "avg_balance": avg_balance,
        },
        categories=categories,
        exam_grade_summary=exam_grade_summary,
        exam_term_summary=exam_term_summary,
        exam_subject_summary=exam_subject_summary,
        recent_exam_batches=recent_exam_batches,
        exam_school_summary=exam_school_summary,
        elections=elections,
        election_candidates=election_candidates,
        library_items=library_items,
        departments=q("SELECT id, name, category, active FROM departments WHERE active=1 ORDER BY name"),
        guardian_links=q("""SELECT gl.*, gu.full_name AS guardian_name, gu.username AS guardian_username, st.full_name AS student_name, st.admission_no
                            FROM guardian_links gl JOIN users gu ON gu.id=gl.guardian_user_id
                            JOIN students st ON st.id=gl.student_id ORDER BY gl.created_at DESC"""),
        archived_users=q("SELECT id, full_name, username, role, title, department, archived_at FROM users WHERE active=0 AND role!='System' ORDER BY archived_at DESC, full_name"),
    )


@app.route("/teacher-dashboard")
@login_required
@role_required("Teacher")
def teacher_dashboard():
    user=current_user(); grade=request.args.get("grade", "").strip()
    grades=[r["grade"] for r in q("SELECT DISTINCT grade FROM students ORDER BY grade")]
    assignments=assignment_rows(grade or None)
    submissions=q("""SELECT s.*, a.title, a.subject, a.grade, st.full_name AS student_name, st.admission_no
                      FROM submissions s JOIN assignments a ON a.id=s.assignment_id
                      JOIN students st ON st.id=s.student_id
                      ORDER BY s.submitted_at DESC, s.id DESC LIMIT 50""")
    messages=q("""SELECT * FROM portal_messages WHERE (sender_role='Parent' OR recipient_role='Teacher') ORDER BY created_at DESC LIMIT 30""")
    available_students=q("SELECT * FROM students WHERE active=1 ORDER BY grade, full_name")
    library_items=q("SELECT * FROM library_items WHERE active=1 ORDER BY category,title LIMIT 80") if school_settings()["library_enabled"] else []
    settings=school_settings(); nav_items=navigation_items("Teacher", settings)
    return render_template("role_dashboard.html", role="Teacher", workspace=workspace_for("Teacher"), grades=grades, selected_grade=grade, assignments=assignments, submissions=submissions, messages=messages, students=available_students, library_items=library_items, teacher_online_url="https://meet.google.com/new", actor_name=user["full_name"], nav_items=nav_items)


@app.route("/student-dashboard")
@login_required
@role_required("Student")
def student_dashboard():
    student=portal_student(request.args.get("student_id", type=int))
    if not student: abort(404)
    assignments=assignment_rows(student["grade"])
    submissions=q("SELECT * FROM submissions WHERE student_id=? ORDER BY submitted_at DESC", (student["id"],))
    results=q("SELECT subject, term, mark, max_mark FROM exam_results WHERE student_id=? ORDER BY term DESC, subject", (student["id"],))
    result_releases=result_release_info(student["id"])
    messages=q("SELECT * FROM portal_messages WHERE recipient_student_id=? OR (recipient_role='Student' AND recipient_student_id IS NULL) ORDER BY created_at DESC LIMIT 30", (student["id"],))
    elections=q("SELECT * FROM elections WHERE visible=1 ORDER BY created_at DESC") if school_settings()["elections_enabled"] else []
    election_candidates={e["id"]:q("SELECT * FROM election_candidates WHERE election_id=? AND active=1 ORDER BY position,name",(e["id"],)) for e in elections}
    voted_positions={(r["election_id"], r["position"]) for r in q("SELECT election_id, position FROM election_votes WHERE voter_user_id=?",(current_user()["id"],))}
    library_items=q("SELECT * FROM library_items WHERE active=1 ORDER BY category,title LIMIT 80") if school_settings()["library_enabled"] else []
    settings=school_settings(); nav_items=navigation_items("Student", settings)
    return render_template("role_dashboard.html", role="Student", workspace=workspace_for("Student"), student=student, assignments=assignments, submissions=submissions, results=results, result_releases=result_releases, messages=messages, elections=elections, election_candidates=election_candidates, voted_positions=voted_positions, library_items=library_items, actor_name=student["full_name"], nav_items=nav_items)


@app.route("/parent-dashboard")
@login_required
@role_required("Parent")
def parent_dashboard():
    if not parent_portal_enabled(): abort(404)
    children=parent_children(current_user())
    if not children: abort(404)
    requested=request.args.get("child_id", type=int)
    child=next((row for row in children if row["id"] == requested), None) if requested else children[0]
    if not child: abort(403)
    assignments=assignment_rows(child["grade"])
    results=q("SELECT subject, term, mark, max_mark FROM exam_results WHERE student_id=? ORDER BY term DESC, subject", (child["id"],))
    result_releases=result_release_info(child["id"])
    submissions=q("""SELECT s.*, a.title, a.subject FROM submissions s JOIN assignments a ON a.id=s.assignment_id WHERE s.student_id=? ORDER BY s.submitted_at DESC""", (child["id"],))
    messages=q("SELECT * FROM portal_messages WHERE recipient_student_id=? ORDER BY created_at DESC LIMIT 30", (child["id"],))
    library_items=q("SELECT * FROM library_items WHERE active=1 ORDER BY category,title LIMIT 80") if school_settings()["library_enabled"] else []
    settings=school_settings(); nav_items=navigation_items("Parent", settings)
    return render_template("role_dashboard.html", role="Parent", workspace=workspace_for("Parent"), child=child, children=children, assignments=assignments, results=results, result_releases=result_releases, submissions=submissions, messages=messages, library_items=library_items, actor_name=child["guardian_name"] or "Parent", nav_items=nav_items)


@app.route("/library")
@login_required
def library():
    if not school_settings()["library_enabled"] and current_user()["role"] not in {"Admin","ICT","Librarian"}: abort(404)
    class_level=request.args.get("class","").strip(); subject=request.args.get("subject","").strip()
    where=["active=1"]; params=[]
    if class_level: where.append("(class_level=? OR class_level='')"); params.append(class_level)
    if subject: where.append("(subject=? OR subject='')"); params.append(subject)
    items=q(f"SELECT * FROM library_items WHERE {' AND '.join(where)} ORDER BY class_level,subject,category,title",params)
    loans=q("""SELECT l.*, i.title, s.full_name AS student_name, s.admission_no FROM library_loans l JOIN library_items i ON i.id=l.item_id JOIN students s ON s.id=l.student_id WHERE l.status='Issued' ORDER BY l.due_date, l.issued_at""")
    classes=[r["grade"] for r in q("SELECT DISTINCT grade FROM students WHERE active=1 ORDER BY grade")]
    subjects=[r["subject"] for r in q("SELECT DISTINCT subject FROM library_items WHERE active=1 AND subject!='' ORDER BY subject")]
    return render_template("library.html", items=items, loans=loans, settings=school_settings(), actor_name=current_user()["full_name"], selected_class=class_level, selected_subject=subject, library_classes=classes, library_subjects=subjects)

@app.route("/librarian-dashboard")
@login_required
@role_required("Librarian","Admin","ICT")
def librarian_dashboard():
    items=q("SELECT * FROM library_items ORDER BY active DESC, category, title")
    loans=q("""SELECT l.*, i.title, s.full_name AS student_name, s.admission_no FROM library_loans l JOIN library_items i ON i.id=l.item_id JOIN students s ON s.id=l.student_id ORDER BY CASE WHEN l.status='Issued' THEN 0 ELSE 1 END, l.due_date DESC""")
    students=q("SELECT * FROM students WHERE active=1 ORDER BY grade,full_name")
    return render_template("library_manager.html", role=current_user()["role"], settings=school_settings(), items=items, loans=loans, students=students, actor_name=current_user()["full_name"])

@app.route("/institution")
@login_required
def institution():
    settings=school_settings()
    return render_template("institution.html", settings=settings, actor_name=current_user()["full_name"], role=current_user()["role"])

@app.route("/institution/save", methods=["POST"])
@login_required
@role_required("Admin")
def institution_save():
    values={k:request.form.get(k,"").strip() for k in ["institution_history","institution_performance","institution_religion","institution_affiliations","institution_help","institution_contact"]}
    image=request.files.get("institution_image"); image_path=school_settings()["institution_image_path"] or ""
    if image and image.filename:
        dest=UPLOAD_DIR/"institution"; dest.mkdir(exist_ok=True); fname=secure_filename(image.filename); out=dest/f"{uuid.uuid4().hex}-{fname}"; image.save(out); image_path="uploads/institution/"+out.name
    execute("UPDATE school_settings SET institution_history=?, institution_performance=?, institution_religion=?, institution_affiliations=?, institution_help=?, institution_contact=?, institution_image_path=?, institution_enabled=1 WHERE id=1",(values["institution_history"],values["institution_performance"],values["institution_religion"],values["institution_affiliations"],values["institution_help"],values["institution_contact"],image_path))
    flash("Institution information updated.","success"); return redirect(url_for("institution"))

@app.route("/ict/features", methods=["POST"])
@login_required
@role_required("Admin","ICT")
def ict_features():
    elections=1 if request.form.get("elections_enabled") else 0; library=1 if request.form.get("library_enabled") else 0
    execute("UPDATE school_settings SET elections_enabled=?, library_enabled=? WHERE id=1",(elections,library)); flash("Module visibility updated.","success"); return redirect(url_for("ict_dashboard") if current_user()["role"]=="ICT" else url_for("admin_dashboard"))

@app.route("/finance-dashboard")
@login_required
@role_required("Finance", "Admin")
def finance_dashboard():
    settings=school_settings()
    payments=q("""SELECT p.*, s.full_name AS student_name, s.admission_no FROM payments p JOIN students s ON s.id=p.student_id ORDER BY p.created_at DESC LIMIT 50""")
    total_income=q("SELECT COALESCE(SUM(amount),0) AS n FROM payments WHERE status='Posted'", one=True)["n"]
    balance=q("SELECT COALESCE(SUM(balance),0) AS n FROM students", one=True)["n"]
    students=q("SELECT * FROM students WHERE active=1 ORDER BY grade, full_name")
    batches=q("""SELECT b.*,u.full_name AS submitted_by_name,COUNT(r.id) AS entries,ROUND(AVG(r.mark),1) AS mean_mark FROM exam_batches b JOIN users u ON u.id=b.submitted_by LEFT JOIN exam_results r ON r.batch_id=b.id GROUP BY b.id ORDER BY b.created_at DESC,b.id DESC LIMIT 30""")
    documents=q("""SELECT d.*,s.full_name AS student_name,s.admission_no FROM portal_documents d JOIN students s ON s.id=d.student_id ORDER BY d.issued_at DESC LIMIT 30""")
    ledger=q("""SELECT f.*,u.full_name AS poster,pu.full_name AS payee_name FROM finance_ledger f JOIN users u ON u.id=f.posted_by LEFT JOIN users pu ON pu.id=f.payee_user_id ORDER BY f.posted_at DESC,f.id DESC LIMIT 60""")
    ledger_income=q("SELECT COALESCE(SUM(amount),0) AS n FROM finance_ledger WHERE status='Posted' AND entry_type='Income'",one=True)["n"]
    ledger_expense=q("SELECT COALESCE(SUM(amount),0) AS n FROM finance_ledger WHERE status='Posted' AND entry_type IN ('Expense','Payroll')",one=True)["n"]
    account=q("SELECT * FROM finance_accounts WHERE id=1",one=True)
    system_balance=float(account["opening_balance"] if account else 0)+float(ledger_income or 0)-float(ledger_expense or 0)
    employees=q("SELECT id,full_name,username,role,title,department FROM users WHERE active=1 AND role NOT IN ('System','Student','Parent') ORDER BY full_name")
    nav_items=navigation_items("Finance", settings)
    return render_template("finance_dashboard.html", role="Finance", workspace=workspace_for("Finance"), settings=settings, payments=payments, total_income=total_income, total_balance=balance, students=students, batches=batches, documents=documents, actor_name=current_user()["full_name"], nav_items=nav_items, ledger=ledger, ledger_income=ledger_income, ledger_expense=ledger_expense, system_balance=system_balance, employees=employees)


@app.route("/ict-dashboard")
@login_required
@role_required("ICT", "Admin")
def ict_dashboard():
    settings=school_settings()
    nav_items=navigation_items("ICT", settings)
    elections=q("SELECT * FROM elections ORDER BY created_at DESC")
    election_candidates={e["id"]:q("SELECT * FROM election_candidates WHERE election_id=? ORDER BY position,name",(e["id"],)) for e in elections}
    library_items=q("SELECT * FROM library_items ORDER BY active DESC,category,title")
    students=q("SELECT * FROM students WHERE active=1 ORDER BY grade,full_name")
    users=q("SELECT id,full_name,username,role,student_id,active FROM users WHERE role!='System' ORDER BY role,full_name")
    return render_template("role_dashboard.html", role="ICT", workspace=workspace_for("ICT"), settings=settings, actor_name=current_user()["full_name"], nav_items=nav_items, onboarding_students=q("SELECT id, full_name, admission_no FROM students ORDER BY full_name"), elections=elections, election_candidates=election_candidates, library_items=library_items, students=students, users=users)


@app.route("/ict/settings", methods=["POST"])
@login_required
@role_required("ICT", "Admin")
def ict_settings():
    school_name=request.form.get("school_name", "School").strip() or "School"
    portal_subtitle=request.form.get("portal_subtitle", "School Portal System").strip() or "School Portal System"
    primary=request.form.get("primary_color", "#10a37f").strip() or "#10a37f"
    accent=request.form.get("accent_color", "#0e8a6d").strip() or "#0e8a6d"
    bg=request.form.get("background_color", "#343541").strip() or "#343541"
    panel=request.form.get("panel_color", "#40414f").strip() or "#40414f"
    sidebar=request.form.get("sidebar_color", panel).strip() or panel
    header=request.form.get("header_color", panel).strip() or panel
    text_color=request.form.get("text_color", "#ececf1").strip() or "#ececf1"
    muted=request.form.get("muted_text_color", "#b5bac7").strip() or "#b5bac7"
    font_family=request.form.get("font_family", "Inter").strip() or "Inter"
    heading_font=request.form.get("heading_font", font_family).strip() or font_family
    try: radius=max(4,min(28,int(request.form.get("radius_px", "12"))))
    except ValueError: radius=12
    try: button_radius=max(4,min(28,int(request.form.get("button_radius_px", "10"))))
    except ValueError: button_radius=10
    theme_mode=request.form.get("theme_mode", "dark").strip().lower() if request.form.get("theme_mode") else "dark"
    if theme_mode not in {"dark","light","system"}: theme_mode="dark"
    menu_order=request.form.get("menu_order", "Home,Assignments,Submissions,Online classes").strip() or "Home,Assignments,Submissions,Online classes"
    labels={k: request.form.get(k, defaults).strip() or defaults for k,defaults in [("home_label","Home"),("assignments_label","Assignments"),("results_label","Results"),("messages_label","Messages"),("finance_label","Finance"),("branding_label","Branding")]}
    custom_css=request.form.get("custom_css", "").strip()[:12000]
    if re.search(r'@import|javascript:|expression\s*\(', custom_css, re.I): custom_css=''
    execute("""UPDATE school_settings SET school_name=?, portal_subtitle=?, primary_color=?, accent_color=?, background_color=?, panel_color=?, sidebar_color=?, header_color=?, text_color=?, muted_text_color=?, font_family=?, heading_font=?, radius_px=?, button_radius_px=?, theme_mode=?, menu_order=?, home_label=?, assignments_label=?, results_label=?, messages_label=?, finance_label=?, branding_label=?, custom_css=? WHERE id=1""", (school_name,portal_subtitle,primary,accent,bg,panel,sidebar,header,text_color,muted,font_family,heading_font,radius,button_radius,theme_mode,menu_order,labels["home_label"],labels["assignments_label"],labels["results_label"],labels["messages_label"],labels["finance_label"],labels["branding_label"],custom_css))
    audit(current_user()["id"], current_user()["full_name"], "Portal Theme Update", f"Institution-wide interface theme updated for {school_name}.")
    flash("The institution-wide interface has been redesigned and saved.", "success")
    return redirect(url_for("ict_dashboard"))


@app.route("/ict/logo", methods=["POST"])
@login_required
@role_required("ICT", "Admin")
def ict_logo():
    file=request.files.get("logo")
    if not file or not file.filename: return redirect(url_for("ict_dashboard"))
    ext=file.filename.rsplit('.',1)[-1].lower() if '.' in file.filename else ''
    if ext not in {"png","jpg","jpeg","webp","svg"}:
        flash("Logo must be PNG, JPG, JPEG, WEBP or SVG.", "danger")
        return redirect(url_for("ict_dashboard"))
    name="school-logo."+ext
    path=UPLOAD_DIR/name; file.save(path)
    execute("UPDATE school_settings SET logo_path=? WHERE id=1", ("uploads/"+name,))
    flash("School logo updated.", "success")
    return redirect(url_for("ict_dashboard"))


@app.route("/ict/background", methods=["POST"])
@login_required
@role_required("ICT", "Admin")
def ict_background():
    file=request.files.get("background")
    if not file or not file.filename: return redirect(url_for("ict_dashboard"))
    ext=file.filename.rsplit('.',1)[-1].lower() if '.' in file.filename else ''
    if ext not in {"png","jpg","jpeg","webp"}:
        flash("Background must be PNG, JPG, JPEG or WEBP.", "danger")
        return redirect(url_for("ict_dashboard"))
    name="school-background."+ext
    file.save(UPLOAD_DIR/name)
    execute("UPDATE school_settings SET background_path=? WHERE id=1", ("uploads/"+name,))
    flash("Portal background updated.", "success")
    return redirect(url_for("ict_dashboard"))


@app.route("/assignments/create", methods=["POST"])
@login_required
@role_required("Teacher", "Staff")
def create_assignment():
    title=request.form.get("title", "").strip(); subject=request.form.get("subject", "").strip(); grade=request.form.get("grade", "").strip()
    if not title or not subject or not grade:
        flash("Title, subject and grade are required.", "danger"); return redirect(url_for("teacher_dashboard"))
    attachment_path=""
    file=request.files.get("attachment")
    if file and file.filename:
        ext=file.filename.rsplit('.',1)[-1].lower() if '.' in file.filename else ''
        if ext not in {"pdf","doc","docx","png","jpg","jpeg","webp"}:
            flash("Assignment files must be Word, PDF or image files.", "danger"); return redirect(url_for("teacher_dashboard"))
        filename=f"assignment-{datetime.now().strftime('%Y%m%d%H%M%S%f')}.{ext}"
        file.save(UPLOAD_DIR/filename); attachment_path="uploads/"+filename
    execute("INSERT INTO assignments(title,subject,grade,description,deadline,attachment_path,posted_by) VALUES(?,?,?,?,?,?,?)", (title,subject,grade,request.form.get("description",""),request.form.get("deadline",""),attachment_path,current_user()["id"]))
    audit(current_user()["id"], current_user()["full_name"], "Post Assignment", f"{title} posted to {grade}.")
    flash("Assignment posted to the selected class.", "success")
    return redirect(url_for("teacher_dashboard", grade=grade))


@app.route("/assignments/<int:assignment_id>/submit", methods=["POST"])
@login_required
@role_required("Student")
def submit_assignment(assignment_id):
    student=portal_student(request.form.get("student_id", type=int))
    assignment=q("SELECT * FROM assignments WHERE id=?", (assignment_id,), one=True)
    if not student or not assignment or assignment["grade"] != student["grade"]: abort(404)
    file=request.files.get("submission")
    path=""
    if file and file.filename:
        ext=file.filename.rsplit('.',1)[-1].lower() if '.' in file.filename else ''
        if ext not in {"pdf","doc","docx","png","jpg","jpeg","webp"}:
            flash("Submission must be Word, PDF or image.", "danger"); return redirect(url_for("student_dashboard", student_id=student["id"]))
        filename=f"submission-{student['id']}-{assignment_id}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}.{ext}"
        file.save(UPLOAD_DIR/filename); path="uploads/"+filename
    execute("INSERT INTO submissions(assignment_id,student_id,attachment_path,note) VALUES(?,?,?,?)", (assignment_id,student["id"],path,request.form.get("note","")))
    flash("Assignment submitted successfully.", "success")
    return redirect(url_for("student_dashboard", student_id=student["id"]))


# -------------------------------------------------------------------
# Elections
# -------------------------------------------------------------------
def election_now_active(election):
    if not election or not election["active"] or not election["visible"]:
        return False
    now=datetime.now().strftime("%Y-%m-%dT%H:%M")
    return (not election["start_at"] or now >= election["start_at"]) and (not election["end_at"] or now <= election["end_at"])

@app.route("/elections/vote", methods=["POST"])
@login_required
@role_required("Student")
def cast_vote():
    if not school_settings()["elections_enabled"]:
        abort(404)
    election_id=request.form.get("election_id", type=int)
    candidate_id=request.form.get("candidate_id", type=int)
    election=q("SELECT * FROM elections WHERE id=?", (election_id,), one=True)
    candidate=q("SELECT * FROM election_candidates WHERE id=? AND election_id=? AND active=1", (candidate_id,election_id), one=True)
    if not election or not candidate or not election_now_active(election):
        flash("This election is not currently open for voting.", "warning")
        return redirect(url_for("student_dashboard")+"#elections")
    position=candidate["position"]
    already=q("SELECT id FROM election_votes WHERE election_id=? AND voter_user_id=? AND position=?",(election_id,current_user()["id"],position),one=True)
    if already:
        flash(f"You have already voted for {position} in this election.", "warning")
        return redirect(url_for("student_dashboard")+"#elections")
    execute("INSERT INTO election_votes(election_id,candidate_id,voter_user_id,position) VALUES(?,?,?,?)", (election_id,candidate_id,current_user()["id"],position))
    audit(current_user()["id"], current_user()["full_name"], "Cast Election Vote", f"Voted in election {election_id}.")
    flash("Your vote was recorded.", "success")
    return redirect(url_for("student_dashboard")+"#elections")

@app.route("/elections/create", methods=["POST"])
@login_required
@role_required("Admin","ICT")
def create_election():
    title=request.form.get("title","").strip(); description=request.form.get("description","").strip(); start_at=request.form.get("start_at") or None; end_at=request.form.get("end_at") or None
    if not title:
        flash("Election title is required.", "danger"); return redirect(request.referrer or url_for("ict_dashboard"))
    eid=execute("INSERT INTO elections(title,description,start_at,end_at,visible,active,created_by) VALUES(?,?,?,?,0,0,?)", (title,description,start_at,end_at,current_user()["id"]))
    audit(current_user()["id"], current_user()["full_name"], "Create Election", title)
    flash("Election created. Add participants, then activate it when ready.", "success")
    return redirect((url_for("admin_dashboard") if current_user()["role"]=="Admin" else url_for("ict_dashboard"))+"#elections")

@app.route("/elections/<int:election_id>/toggle", methods=["POST"])
@login_required
@role_required("Admin","ICT")
def toggle_election(election_id):
    election=q("SELECT * FROM elections WHERE id=?",(election_id,),one=True)
    if not election: abort(404)
    visible=1 if request.form.get("visible") else 0
    active=1 if request.form.get("active") else 0
    execute("UPDATE elections SET visible=?, active=? WHERE id=?",(visible,active,election_id))
    flash("Election visibility/status updated.","success")
    return redirect((url_for("admin_dashboard") if current_user()["role"]=="Admin" else url_for("ict_dashboard"))+"#elections")

@app.route("/elections/candidate/add", methods=["POST"])
@login_required
@role_required("Admin","ICT")
def add_election_candidate():
    election_id=request.form.get("election_id",type=int); name=request.form.get("name","").strip(); position=request.form.get("position","").strip(); manifesto=request.form.get("manifesto","").strip()
    if not election_id or not name or not position:
        flash("Election, participant name and position are required.","danger"); return redirect(request.referrer or url_for("ict_dashboard"))
    file=request.files.get("image"); image_path=""
    if file and file.filename:
        fname=secure_filename(file.filename); dest=UPLOAD_DIR/"elections"; dest.mkdir(exist_ok=True); out=dest/f"{uuid.uuid4().hex}-{fname}"; file.save(out); image_path="uploads/elections/"+out.name
    execute("INSERT INTO election_candidates(election_id,name,position,manifesto,image_path) VALUES(?,?,?,?,?)",(election_id,name,position,manifesto,image_path))
    flash("Election participant added.","success")
    return redirect((url_for("admin_dashboard") if current_user()["role"]=="Admin" else url_for("ict_dashboard"))+"#elections")

@app.route("/elections/candidates/upload", methods=["POST"])
@login_required
@role_required("Admin","ICT")
def upload_election_candidates():
    election_id=request.form.get("election_id",type=int); file=request.files.get("participants")
    if not election_id or not file or not file.filename:
        flash("Select an election and CSV participant file.","danger"); return redirect(request.referrer or url_for("ict_dashboard"))
    try:
        text=file.read().decode("utf-8-sig")
        reader=csv.DictReader(io.StringIO(text)); count=0
        for row in reader:
            name=(row.get("name") or row.get("Name") or "").strip(); position=(row.get("position") or row.get("Position") or "").strip(); manifesto=(row.get("manifesto") or row.get("Manifesto") or "").strip()
            if not name or not position: continue
            execute("INSERT INTO election_candidates(election_id,name,position,manifesto) VALUES(?,?,?,?)",(election_id,name,position,manifesto)); count+=1
        flash(f"Imported {count} election participants.","success")
    except Exception as exc:
        flash(f"Could not read participant file: {exc}","danger")
    return redirect((url_for("admin_dashboard") if current_user()["role"]=="Admin" else url_for("ict_dashboard"))+"#elections")

# -------------------------------------------------------------------
# Library
# -------------------------------------------------------------------
@app.route("/library/add", methods=["POST"])
@login_required
@role_required("Admin","ICT","Librarian")
def add_library_item():
    title=request.form.get("title","").strip(); category=request.form.get("category","Book").strip() or "Book"
    author=request.form.get("author","").strip(); code=request.form.get("item_code","").strip(); location=request.form.get("location","").strip()
    resource_type=request.form.get("resource_type","Physical").strip(); qty=max(1,request.form.get("quantity",type=int) or 1)
    description=request.form.get("description","").strip(); external_url=request.form.get("external_url","").strip()
    youtube_url=request.form.get("youtube_url","").strip(); source_url=request.form.get("source_url","").strip(); source_name=request.form.get("source_name","").strip()
    class_level=request.form.get("class_level","").strip(); subject=request.form.get("subject","").strip()
    if not title: flash("Library title is required.","danger"); return redirect(request.referrer or url_for("librarian_dashboard"))
    file=request.files.get("resource"); file_path=""
    if file and file.filename:
        fname=secure_filename(file.filename); dest=UPLOAD_DIR/"library"; dest.mkdir(exist_ok=True); out=dest/f"{uuid.uuid4().hex}-{fname}"; file.save(out); file_path="uploads/library/"+out.name; resource_type="Digital"
    image=request.files.get("resource_image"); image_path=""
    if image and image.filename:
        ext=image.filename.rsplit('.',1)[-1].lower() if '.' in image.filename else ''
        if ext not in {"png","jpg","jpeg","webp"}: flash("Resource image must be PNG/JPG/JPEG/WEBP.","danger"); return redirect(request.referrer or url_for("librarian_dashboard"))
        dest=UPLOAD_DIR/"library"; dest.mkdir(exist_ok=True); out=dest/f"{uuid.uuid4().hex}-cover.{ext}"; image.save(out); image_path="uploads/library/"+out.name
    execute("""INSERT INTO library_items(title,category,author,item_code,quantity,available_quantity,location,resource_type,file_path,external_url,description,created_by,class_level,subject,image_path,youtube_url,source_url,source_name) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(title,category,author,code,qty,qty,location,resource_type,file_path,external_url,description,current_user()["id"],class_level,subject,image_path,youtube_url,source_url,source_name))
    audit(current_user()["id"],current_user()["full_name"],"Library Resource Added",f"Added {title} for class '{class_level or 'All'}' and subject '{subject or 'General'}'.")
    flash("Library resource added to the institutional catalogue.","success")
    return redirect((url_for("ict_dashboard") if current_user()["role"]=="ICT" else url_for("librarian_dashboard") if current_user()["role"]=="Librarian" else url_for("admin_dashboard"))+"#library")

@app.route("/library/<int:item_id>/remove", methods=["POST"])
@login_required
@role_required("Admin","ICT","Librarian")
def remove_library_item(item_id):
    execute("UPDATE library_items SET active=0 WHERE id=?",(item_id,)); flash("Library item removed from the catalogue.","success"); return redirect(request.referrer or url_for("librarian_dashboard"))

@app.route("/library/loan", methods=["POST"])
@login_required
@role_required("Admin","ICT","Librarian")
def issue_library_item():
    item_id=request.form.get("item_id",type=int); student_id=request.form.get("student_id",type=int); due=request.form.get("due_date") or None
    item=q("SELECT * FROM library_items WHERE id=? AND active=1",(item_id,),one=True); student=q("SELECT * FROM students WHERE id=? AND active=1",(student_id,),one=True)
    if not item or not student or item["available_quantity"]<1:
        flash("Item unavailable or student invalid.","danger"); return redirect(request.referrer or url_for("librarian_dashboard"))
    execute("INSERT INTO library_loans(item_id,student_id,issued_by,due_date) VALUES(?,?,?,?)",(item_id,student_id,current_user()["id"],due))
    execute("UPDATE library_items SET available_quantity=available_quantity-1 WHERE id=?",(item_id,)); flash("Library item issued.","success"); return redirect(request.referrer or url_for("librarian_dashboard"))

@app.route("/library/loan/<int:loan_id>/return", methods=["POST"])
@login_required
@role_required("Admin","ICT","Librarian")
def return_library_item(loan_id):
    loan=q("SELECT * FROM library_loans WHERE id=? AND status='Issued'",(loan_id,),one=True)
    if not loan: abort(404)
    execute("UPDATE library_loans SET returned_at=CURRENT_TIMESTAMP,status='Returned' WHERE id=?",(loan_id,)); execute("UPDATE library_items SET available_quantity=available_quantity+1 WHERE id=?",(loan["item_id"],)); flash("Library item returned.","success"); return redirect(request.referrer or url_for("librarian_dashboard"))

@app.route("/messages/send", methods=["POST"])
@login_required
def send_message():
    user=current_user(); recipient_role=request.form.get("recipient_role", "Teacher").strip()
    student_id=request.form.get("recipient_student_id", type=int)
    body=request.form.get("body", "").strip()
    allowed_pairs={("Teacher","Parent"),("Parent","Teacher")}
    if not body or (user["role"], recipient_role) not in allowed_pairs:
        flash("That messaging route is not available for this account.", "danger")
        return redirect(request.referrer or url_for("index"))
    student=q("SELECT id FROM students WHERE id=? AND active=1", (student_id,), one=True) if student_id else None
    if not student:
        flash("Select a valid pupil for this message.", "danger")
        return redirect(request.referrer or url_for("index"))
    if user["role"] == "Parent" and not can_access_student(student_id):
        abort(403)
    execute("INSERT INTO portal_messages(sender_role,sender_name,recipient_role,recipient_student_id,body) VALUES(?,?,?,?,?)", (user["role"], user["full_name"], recipient_role, student_id, body))
    flash("Message sent.", "success")
    return redirect(request.referrer or url_for("index"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user = current_user()
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        password = request.form.get("password", "")
        if not full_name:
            flash("Full name is required.", "danger")
        else:
            if password:
                if len(password) < 4:
                    flash("Password must be at least 4 characters.", "danger")
                    return redirect(url_for("profile"))
                execute("UPDATE users SET full_name = ?, password_hash = ? WHERE id = ?", (full_name, generate_password_hash(password), user["id"]))
            else:
                execute("UPDATE users SET full_name = ? WHERE id = ?", (full_name, user["id"]))
            session["user_id"] = user["id"]
            audit(user["id"], full_name, "Profile Update", "Profile details updated.")
            flash("Profile updated successfully.", "success")
            return redirect(url_for("profile"))
    profile_user = q("SELECT id, full_name, username, role, created_at FROM users WHERE id = ?", (user["id"],), one=True)
    return render_template("profile.html", profile_user=profile_user, workspace=workspace_for(profile_user["role"]))


@app.route("/settings", methods=["POST"])
@login_required
@role_required("Admin", "ICT")
def save_settings():
    school_name = request.form.get("school_name", "").strip() or "School"
    admission_prefix = request.form.get("admission_prefix", "").strip() or "ADM-"
    admission_suffix = request.form.get("admission_suffix", "").strip()
    student_name_prefix = request.form.get("student_name_prefix", "").strip()
    student_name_suffix = request.form.get("student_name_suffix", "").strip()
    currency_code = request.form.get("currency_code", "").strip() or "KES"
    try:
        school_fee = float(request.form.get("school_fee", "0") or 0)
    except ValueError:
        school_fee = 0.0
    execute(
        """
        UPDATE school_settings
        SET school_name = ?, admission_prefix = ?, admission_suffix = ?, student_name_prefix = ?, student_name_suffix = ?, currency_code = ?, school_fee = ?
        WHERE id = 1
        """,
        (school_name, admission_prefix, admission_suffix, student_name_prefix, student_name_suffix, currency_code, school_fee),
    )
    audit(current_user()["id"], current_user()["full_name"], "Update Settings", f"School settings updated for {school_name}.")
    flash("School settings updated.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/students/add", methods=["POST"])
@login_required
@role_required("Admin", "ICT")
def add_student():
    admission_no = request.form.get("admission_no", "").strip()
    full_name = request.form.get("full_name", "").strip()
    grade = request.form.get("grade", "").strip()
    guardian_name = request.form.get("guardian_name", "").strip()
    guardian_phone = request.form.get("guardian_phone", "").strip()
    guardian_email = request.form.get("guardian_email", "").strip()
    alt_guardian_name = request.form.get("alt_guardian_name", "").strip()
    alt_guardian_phone = request.form.get("alt_guardian_phone", "").strip()
    alt_guardian_email = request.form.get("alt_guardian_email", "").strip()
    student_phone = request.form.get("student_phone", "").strip()
    student_email = request.form.get("student_email", "").strip()
    medical_condition = request.form.get("medical_condition", "").strip()
    allergies = request.form.get("allergies", "").strip()
    special_info = request.form.get("special_info", "").strip()
    notes = request.form.get("notes", "").strip()
    if not full_name or not grade:
        flash("Student name and grade are required.", "danger")
        return redirect(request.referrer or url_for("dashboard"))

    settings = school_settings()
    if not admission_no:
        admission_no = next_admission_no()
    if settings["student_name_prefix"]:
        full_name = f"{settings['student_name_prefix']} {full_name}".strip()
    if settings["student_name_suffix"]:
        full_name = f"{full_name} {settings['student_name_suffix']}".strip()

    try:
        starting_balance = float(settings["school_fee"] or 0)
        execute(
            """
            INSERT INTO students(
                admission_no, full_name, grade,
                guardian_name, guardian_phone, guardian_email,
                alt_guardian_name, alt_guardian_phone, alt_guardian_email,
                student_phone, student_email, medical_condition, allergies, special_info, notes,
                payment_status, balance, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pending', ?, 1)
            """,
            (
                admission_no,
                full_name,
                grade,
                guardian_name,
                guardian_phone,
                guardian_email,
                alt_guardian_name,
                alt_guardian_phone,
                alt_guardian_email,
                student_phone,
                student_email,
                medical_condition,
                allergies,
                special_info,
                notes,
                starting_balance,
            ),
        )
        audit(current_user()["id"], current_user()["full_name"], "Add Student", f"{full_name} ({admission_no}) created.")
        flash("Student added.", "success")
    except sqlite3.IntegrityError:
        flash("Admission number already exists.", "danger")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/students/<int:student_id>")
@login_required
def student_profile(student_id:int):
    student=q("SELECT * FROM students WHERE id=?",(student_id,),one=True)
    if not student: abort(404)
    user=current_user(); role=user["role"]
    if role in {"Student","Parent"} and not can_access_student(student_id): abort(403)
    if role not in {"Admin","ICT","Teacher","Finance","Librarian","Student","Parent"}: abort(403)
    guardians=q("""SELECT gu.id,gu.full_name,gu.username,gu.phone,gu.email,gl.relationship,gl.is_primary FROM guardian_links gl JOIN users gu ON gu.id=gl.guardian_user_id WHERE gl.student_id=? AND gl.active=1 ORDER BY gl.is_primary DESC,gu.full_name""",(student_id,))
    payments=q("SELECT p.*,u.full_name AS recorder FROM payments p LEFT JOIN users u ON u.id=p.recorded_by WHERE p.student_id=? ORDER BY p.created_at DESC,p.id DESC",(student_id,))
    records=q("SELECT r.*,u.full_name AS author,u.role AS author_role FROM student_records r JOIN users u ON u.id=r.author_user_id WHERE r.student_id=? AND (r.visible_to_parent=1 OR ? IN ('Admin','Teacher','ICT') OR r.author_user_id=?) ORDER BY r.created_at DESC,r.id DESC",(student_id,role,user["id"]))
    results=q("SELECT * FROM exam_results WHERE student_id=? ORDER BY term DESC,subject",(student_id,))
    awards=[r for r in records if r['category']=='Award']; discipline=[r for r in records if r['category']=='Indiscipline']
    return render_template('student_profile.html',student=student,guardians=guardians,payments=payments,records=records,results=results,awards=awards,discipline=discipline,settings=school_settings(),actor_name=user['full_name'],role=role,can_edit=role in {'Admin','ICT'},can_write_record=role in {'Admin','Teacher'},parent_visible_count=sum(1 for r in records if r['visible_to_parent']))

@app.route("/students/<int:student_id>/record", methods=["POST"])
@login_required
@role_required("Admin","Teacher")
def add_student_record(student_id:int):
    student=q("SELECT id,full_name FROM students WHERE id=?",(student_id,),one=True)
    if not student: abort(404)
    category=request.form.get('category','General').strip() or 'General'
    title=request.form.get('title','').strip(); content=request.form.get('content','').strip()
    if not title or not content:
        flash('A record title and details are required.','danger'); return redirect(url_for('student_profile',student_id=student_id))
    visible=1 if request.form.get('visible_to_parent')=='1' and current_user()['role'] in {'Admin','Teacher'} else 0
    execute("INSERT INTO student_records(student_id,author_user_id,category,title,content,visible_to_parent) VALUES(?,?,?,?,?,?)",(student_id,current_user()['id'],category,title,content,visible))
    audit(current_user()['id'],current_user()['full_name'],'Student Record',f"Added {category} record for {student['full_name']}; parent-visible={visible}.")
    flash('Student record added.','success'); return redirect(url_for('student_profile',student_id=student_id))

@app.route("/students/<int:student_id>/record/<int:record_id>/visibility", methods=["POST"])
@login_required
@role_required("Admin","Teacher")
def toggle_student_record_visibility(student_id:int,record_id:int):
    row=q("SELECT * FROM student_records WHERE id=? AND student_id=?",(record_id,student_id),one=True)
    if not row: abort(404)
    visible=1 if request.form.get('visible_to_parent')=='1' else 0
    execute("UPDATE student_records SET visible_to_parent=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",(visible,record_id))
    audit(current_user()['id'],current_user()['full_name'],'Student Record Visibility',f"Record #{record_id} visibility set to {visible}.")
    flash('Parent visibility updated.','success'); return redirect(url_for('student_profile',student_id=student_id))

@app.route("/students/search")
@login_required
@role_required("Admin","Teacher","ICT")
def student_search():
    term=request.args.get('q','').strip()
    rows=q("SELECT id,full_name,admission_no,grade,payment_status,balance,active FROM students WHERE full_name LIKE ? OR admission_no LIKE ? ORDER BY full_name LIMIT 100",(f'%{term}%',f'%{term}%')) if term else q("SELECT id,full_name,admission_no,grade,payment_status,balance,active FROM students WHERE active=1 ORDER BY full_name LIMIT 100")
    return render_template('student_search.html',rows=rows,term=term,settings=school_settings(),role=current_user()['role'],actor_name=current_user()['full_name'])

@app.route("/students/<int:student_id>/update", methods=["POST"])
@login_required
@role_required("Admin", "ICT")
def update_student(student_id: int):
    student = q("SELECT * FROM students WHERE id = ?", (student_id,), one=True)
    if not student:
        abort(404)
    fields = {
        "full_name": request.form.get("full_name", student["full_name"]).strip(),
        "admission_no": request.form.get("admission_no", student["admission_no"]).strip(),
        "grade": request.form.get("grade", student["grade"]).strip(),
        "guardian_name": request.form.get("guardian_name", "").strip(),
        "guardian_phone": request.form.get("guardian_phone", "").strip(),
        "guardian_email": request.form.get("guardian_email", "").strip(),
        "alt_guardian_name": request.form.get("alt_guardian_name", "").strip(),
        "alt_guardian_phone": request.form.get("alt_guardian_phone", "").strip(),
        "alt_guardian_email": request.form.get("alt_guardian_email", "").strip(),
        "student_phone": request.form.get("student_phone", "").strip(),
        "student_email": request.form.get("student_email", "").strip(),
        "medical_condition": request.form.get("medical_condition", "").strip(),
        "allergies": request.form.get("allergies", "").strip(),
        "special_info": request.form.get("special_info", "").strip(),
        "notes": request.form.get("notes", "").strip(),
        "payment_status": request.form.get("payment_status", "Pending"),
        "balance": request.form.get("balance", "0").strip(),
        "active": 1 if request.form.get("active") == "1" else 0,
    }
    execute(
        """
        UPDATE students SET
            admission_no = ?, full_name = ?, grade = ?, guardian_name = ?, guardian_phone = ?, guardian_email = ?,
            alt_guardian_name = ?, alt_guardian_phone = ?, alt_guardian_email = ?, student_phone = ?, student_email = ?,
            medical_condition = ?, allergies = ?, special_info = ?, notes = ?,
            payment_status = ?, balance = ?, active = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            fields["admission_no"],
            fields["full_name"],
            fields["grade"],
            fields["guardian_name"],
            fields["guardian_phone"],
            fields["guardian_email"],
            fields["alt_guardian_name"],
            fields["alt_guardian_phone"],
            fields["alt_guardian_email"],
            fields["student_phone"],
            fields["student_email"],
            fields["medical_condition"],
            fields["allergies"],
            fields["special_info"],
            fields["notes"],
            fields["payment_status"],
            float(fields["balance"] or 0),
            fields["active"],
            student_id,
        ),
    )
    audit(current_user()["id"], current_user()["full_name"], "Edit Student", f"Student {student['admission_no']} updated.")
    flash("Student updated.", "success")
    return redirect(request.referrer or url_for("dashboard", student_id=student_id))


@app.route("/students/<int:student_id>/delete", methods=["POST"])
@login_required
@role_required("Admin")
def delete_student(student_id: int):
    student = q("SELECT * FROM students WHERE id = ?", (student_id,), one=True)
    if not student:
        abort(404)
    execute("DELETE FROM students WHERE id = ?", (student_id,))
    audit(current_user()["id"], current_user()["full_name"], "Delete Student", f"Student {student['admission_no']} deleted.")
    flash("Pupil deleted.", "success")
    return redirect(request.referrer or url_for("admin_dashboard"))


@app.route("/payments/add", methods=["POST"])
@login_required
@role_required("Finance", "Admin")
def add_payment():
    admission_no = request.form.get("admission_no", "").strip()
    amount = request.form.get("amount", "").strip()
    method = request.form.get("method", "").strip()
    reference_no = request.form.get("reference_no", "").strip()
    student = q("SELECT * FROM students WHERE admission_no = ?", (admission_no,), one=True)
    if not student:
        flash("Student admission number not found.", "danger")
        return redirect(request.referrer or url_for("dashboard"))
    try:
        amount_f = float(amount)
        if amount_f <= 0:
            raise ValueError
    except ValueError:
        flash("Enter a valid payment amount.", "danger")
        return redirect(request.referrer or url_for("dashboard"))
    if method not in {"Cash", "M-Pesa", "Bank", "Cheque"}:
        flash("Select a valid payment method.", "danger")
        return redirect(request.referrer or url_for("dashboard"))
    execute(
        "INSERT INTO payments(student_id, amount, method, reference_no, recorded_by, status) VALUES (?, ?, ?, ?, ?, 'Posted')",
        (student["id"], amount_f, method, reference_no, current_user()["id"]),
    )
    new_balance = max(0, float(student["balance"]) - amount_f)
    new_status = "Paid" if new_balance == 0 else "Pending"
    execute("UPDATE students SET balance = ?, payment_status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_balance, new_status, student["id"]))
    audit(current_user()["id"], current_user()["full_name"], "Record Payment", f"{amount_f:.2f} recorded for {student['admission_no']} using {method}.")
    flash("Payment recorded.", "success")
    return redirect(request.referrer or url_for("dashboard", student_id=student["id"]))


@app.route("/payments/<int:payment_id>/reverse", methods=["POST"])
@login_required
@role_required("Admin", "Finance")
def reverse_payment(payment_id: int):
    payment = q("SELECT * FROM payments WHERE id = ?", (payment_id,), one=True)
    if not payment:
        abort(404)
    if payment["status"] == "Reversed":
        flash("Payment is already reversed.", "warning")
        return redirect(request.referrer or url_for("admin_dashboard"))
    execute("UPDATE payments SET status = 'Reversed', reversed_at = CURRENT_TIMESTAMP WHERE id = ?", (payment_id,))
    student = q("SELECT * FROM students WHERE id = ?", (payment["student_id"],), one=True)
    if student:
        updated_balance = float(student["balance"]) + float(payment["amount"])
        execute("UPDATE students SET balance = ?, payment_status = 'Pending', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (updated_balance, student["id"]))
    audit(current_user()["id"], current_user()["full_name"], "Reverse Payment", f"Payment #{payment_id} reversed.")
    flash("Payment reversed.", "success")
    return redirect(request.referrer or url_for("admin_dashboard"))

@app.route("/finance/payment", methods=["POST"])
@login_required
@role_required("Finance", "Admin")
def finance_record_payment():
    student=q("SELECT * FROM students WHERE id=?",(request.form.get("student_id",type=int),),one=True)
    if not student: abort(404)
    try: amount=float(request.form.get("amount","0") or 0)
    except ValueError: amount=0
    if amount<=0: flash("Enter a valid payment amount.","danger"); return redirect(url_for("finance_dashboard"))
    method=request.form.get("method","M-Pesa").strip(); reference=request.form.get("reference_no","").strip(); receipt_path=""
    file=request.files.get("receipt")
    if file and file.filename:
        ext=file.filename.rsplit('.',1)[-1].lower() if '.' in file.filename else ''
        if ext not in {"pdf","png","jpg","jpeg","webp"}: flash("Receipt must be PDF or image.","danger"); return redirect(url_for("finance_dashboard"))
        receipt_dir=UPLOAD_DIR/"receipts"; receipt_dir.mkdir(exist_ok=True)
        name=f"receipt-{student['id']}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}.{ext}"; file.save(receipt_dir/name); receipt_path="uploads/receipts/"+name
    payment_id=execute("INSERT INTO payments(student_id,amount,method,reference_no,recorded_by,status,receipt_path) VALUES(?,?,?,?,?,'Posted',?)",(student['id'],amount,method,reference,current_user()['id'],receipt_path))
    new_balance=max(0,float(student['balance'] or 0)-amount); execute("UPDATE students SET balance=?,payment_status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(new_balance,'Paid' if new_balance==0 else 'Pending',student['id']))
    audit(current_user()['id'],current_user()['full_name'],'Finance Payment',f"Payment #{payment_id}: {amount:.2f} for {student['admission_no']}; balance {new_balance:.2f}.")
    flash("Payment posted and the student/parent balance has been updated.","success"); return redirect(url_for("finance_dashboard"))

@app.route("/finance/results/<int:batch_id>/approve", methods=["POST"])
@login_required
@role_required("Finance", "Admin")
def finance_approve_results(batch_id):
    batch=q("SELECT * FROM exam_batches WHERE id=?",(batch_id,),one=True)
    if not batch: abort(404)
    settings=school_settings(); limit=float(settings['result_download_balance_limit'] or 0)
    balances=q("SELECT s.full_name,s.balance FROM students s JOIN exam_results r ON r.student_id=s.id WHERE r.batch_id=? GROUP BY s.id",(batch_id,))
    over=[r for r in balances if float(r['balance'] or 0)>limit]; override=request.form.get('override')=='1' and current_user()['role']=='Admin'
    if over and not override:
        flash(f"Approval blocked: {len(over)} student(s) exceed the result-release balance limit of {settings['currency_code']} {limit:,.0f}.","danger"); return redirect(url_for('finance_dashboard'))
    note=request.form.get('finance_note','').strip() or ("Approved within balance policy." if not override else "Approved by Admin override.")
    execute("UPDATE exam_batches SET finance_status='Approved',finance_note=?,approved_by=?,approved_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",(note,current_user()['id'],batch_id)); audit(current_user()['id'],current_user()['full_name'],'Approve Results',f"Exam batch #{batch_id} approved. {note}"); flash("Results approved for release.","success"); return redirect(url_for('finance_dashboard'))

@app.route("/finance/results/<int:batch_id>/reject", methods=["POST"])
@login_required
@role_required("Finance", "Admin")
def finance_reject_results(batch_id):
    batch=q("SELECT id FROM exam_batches WHERE id=?",(batch_id,),one=True)
    if not batch: abort(404)
    note=request.form.get('finance_note','').strip() or "Finance did not approve this result batch."
    execute("UPDATE exam_batches SET finance_status='Rejected',finance_note=?,approved_by=?,approved_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",(note,current_user()['id'],batch_id)); audit(current_user()['id'],current_user()['full_name'],'Reject Results',f"Exam batch #{batch_id} rejected. {note}"); flash("Results marked not approved for release.","warning"); return redirect(url_for('finance_dashboard'))

@app.route("/finance/policy", methods=["POST"])
@login_required
@role_required("Finance", "Admin")
def finance_policy():
    try: limit=max(0,float(request.form.get('result_download_balance_limit','500') or 500))
    except ValueError: limit=500
    execute("UPDATE school_settings SET result_download_balance_limit=? WHERE id=1",(limit,)); audit(current_user()['id'],current_user()['full_name'],'Finance Policy',f"Result download balance limit set to {limit:.2f}."); flash("Result download threshold updated.","success"); return redirect(url_for('finance_dashboard'))

@app.route("/report-card/<int:student_id>/<int:batch_id>")
@login_required
def report_card(student_id:int,batch_id:int):
    student=q("SELECT * FROM students WHERE id=?",(student_id,),one=True); batch=q("SELECT * FROM exam_batches WHERE id=?",(batch_id,),one=True)
    if not student or not batch: abort(404)
    role=current_user()['role']
    if role in {'Student','Parent'} and not can_access_student(student_id): abort(403)
    if role=='Parent' and not parent_portal_enabled(): abort(403)
    limit=float(school_settings()['result_download_balance_limit'] or 0)
    eligible=batch['finance_status']=='Approved' and float(student['balance'] or 0)<=limit
    if role in {'Student','Parent'} and not eligible:
        flash('This online report card is awaiting Finance clearance.','warning'); return redirect(request.referrer or url_for('dashboard'))
    results=q("SELECT subject,term,mark,max_mark FROM exam_results WHERE student_id=? AND batch_id=? ORDER BY subject",(student_id,batch_id))
    return render_template('report_card.html',student=student,batch=batch,results=results,eligible=eligible,settings=school_settings())

@app.route("/results/<int:student_id>/<int:batch_id>/download")
@login_required
def download_result(student_id,batch_id):
    student=q("SELECT * FROM students WHERE id=?",(student_id,),one=True); batch=q("SELECT * FROM exam_batches WHERE id=?",(batch_id,),one=True)
    if not student or not batch: abort(404)
    if current_user()['role'] not in {'Admin','Finance','Student','Parent'}: abort(403)
    if current_user()['role'] in {'Student','Parent'} and not can_access_student(student_id): abort(403)
    if batch['finance_status']!='Approved' or float(student['balance'] or 0)>float(school_settings()['result_download_balance_limit'] or 0):
        flash("This result is not available for download under the current finance release policy.","warning"); return redirect(request.referrer or url_for('student_dashboard',student_id=student_id))
    results=q("SELECT subject,mark,max_mark FROM exam_results WHERE student_id=? AND batch_id=? ORDER BY subject",(student_id,batch_id))
    doc=q("SELECT * FROM portal_documents WHERE document_type='Result' AND student_id=? AND batch_id=? AND status='Valid' ORDER BY id DESC LIMIT 1",(student_id,batch_id),one=True)
    if not doc:
        token=make_qr_token('Result',student_id); execute("INSERT INTO portal_documents(document_type,student_id,batch_id,qr_token,issued_by) VALUES(?,?,?,?,?)",('Result',student_id,batch_id,token,current_user()['id'])); doc=q("SELECT * FROM portal_documents WHERE qr_token=?",(token,),one=True)
    pdf=generate_result_pdf(student,batch,results,doc,create_qr_file(doc['qr_token']))
    return send_file(pdf,as_attachment=True,download_name=f"Result-{student['admission_no']}-{batch['term'].replace(' ','_')}.pdf",mimetype='application/pdf')

@app.route("/exam-cards/create", methods=["POST"])
@login_required
@role_required("Admin", "Finance")
def create_exam_cards():
    student=q("SELECT * FROM students WHERE id=?",(request.form.get('student_id',type=int),),one=True)
    if not student: abort(404)
    batch_name=request.form.get('batch_name','Current Examination').strip() or 'Current Examination'; uploaded=request.files.get('exam_card'); token=make_qr_token('Exam Card',student['id']); file_path=''
    if uploaded and uploaded.filename:
        ext=uploaded.filename.rsplit('.',1)[-1].lower() if '.' in uploaded.filename else ''
        if ext not in {'pdf','png','jpg','jpeg','webp'}: flash("Exam card must be PDF or image.","danger"); return redirect(url_for('finance_dashboard'))
        name=f"exam-card-{student['id']}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}.{ext}"; uploaded.save(UPLOAD_DIR/name); raw_path=UPLOAD_DIR/name
        file_path=embed_qr_in_document(raw_path,create_qr_file(token),token) or ('uploads/'+name)
    doc_id=execute("INSERT INTO portal_documents(document_type,student_id,qr_token,file_path,issued_by) VALUES(?,?,?,?,?)",('Exam Card',student['id'],token,file_path,current_user()['id']))
    audit(current_user()['id'],current_user()['full_name'],'Exam Card',f"Exam card prepared for {student['admission_no']}.")
    if uploaded and file_path: flash("Exam card uploaded and registered. The verification QR is associated with this official document record.","success"); return redirect(url_for('finance_dashboard'))
    flash("Exam card generated and registered.","success"); return redirect(url_for('download_exam_card',document_id=doc_id,batch_name=batch_name))

@app.route("/exam-cards/<int:document_id>/download")
@login_required
def download_exam_card(document_id):
    doc=q("SELECT d.*,s.full_name,s.admission_no,s.grade,s.balance FROM portal_documents d JOIN students s ON s.id=d.student_id WHERE d.id=? AND d.document_type='Exam Card'",(document_id,),one=True)
    if not doc: abort(404)
    if current_user()["role"] in {"Student", "Parent"} and not can_access_student(doc["student_id"]): abort(403)
    if doc['file_path']: return send_file(BASE_DIR/doc['file_path'],as_attachment=True,download_name=Path(doc['file_path']).name)
    pdf=generate_exam_card_pdf(doc,request.args.get('batch_name','Current Examination'),doc,create_qr_file(doc['qr_token']))
    return send_file(pdf,as_attachment=True,download_name=f"Exam-Card-{doc['admission_no']}.pdf",mimetype='application/pdf')

@app.route("/verify/<token>")
def verify_document(token):
    doc=q("SELECT d.*,s.full_name,s.admission_no,s.grade FROM portal_documents d JOIN students s ON s.id=d.student_id WHERE d.qr_token=?",(token,),one=True)
    if not doc: return render_template('error.html',message='Verification token not found.'),404
    status='AUTHENTIC' if doc['status']=='Valid' else 'REVOKED'
    return render_template('error.html',message=f"Document {status}: {doc['document_type']} — {doc['full_name']} ({doc['admission_no']}). Token: {doc['qr_token']}")

@app.route("/exams/submit", methods=["POST"])
@login_required
@role_required("Teacher")
def submit_exams():
    grade = request.form.get("grade", "").strip()
    term = request.form.get("term", "").strip() or "Term 1"
    payload_raw = request.form.get("exam_payload", "").strip()
    if not grade:
        flash("Choose a grade first.", "danger")
        return redirect(request.referrer or url_for("dashboard"))
    if not payload_raw:
        flash("Add at least one subject and a few marks before submitting.", "danger")
        return redirect(request.referrer or url_for("dashboard", exam_grade=grade))
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        flash("Could not read the marks submission.", "danger")
        return redirect(request.referrer or url_for("dashboard", exam_grade=grade))

    subjects = [str(item).strip() for item in payload.get("subjects", []) if str(item).strip()]
    rows = payload.get("rows", [])
    if not subjects or not rows:
        flash("Add at least one subject and one pupil mark.", "danger")
        return redirect(request.referrer or url_for("dashboard", exam_grade=grade))

    valid_students = {row["id"] for row in q("SELECT id FROM students WHERE grade = ?", (grade,))}
    batch_id = execute(
        "INSERT INTO exam_batches(grade, term, submitted_by, status) VALUES (?, ?, ?, 'Submitted')",
        (grade, term, current_user()["id"]),
    )

    saved = 0
    for row in rows:
        try:
            student_id = int(row.get("student_id"))
        except (TypeError, ValueError):
            continue
        if student_id not in valid_students:
            continue
        student = q("SELECT id, full_name, admission_no, grade FROM students WHERE id = ?", (student_id,), one=True)
        if not student:
            continue
        marks = row.get("marks", {}) or {}
        for subject in subjects:
            raw_mark = marks.get(subject, "")
            if raw_mark in (None, ""):
                continue
            try:
                mark = float(raw_mark)
            except (TypeError, ValueError):
                continue
            mark = max(0.0, min(100.0, mark))
            execute(
                """
                INSERT INTO exam_results(
                    batch_id, grade, term, subject, student_id, student_name, admission_no, mark, max_mark, submitted_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 100, ?)
                """,
                (batch_id, grade, term, subject, student_id, student["full_name"], student["admission_no"], mark, current_user()["id"]),
            )
            saved += 1

    audit(current_user()["id"], current_user()["full_name"], "Submit Exam Results", f"{grade} {term} submitted with {saved} mark entries.")
    flash(f"Exam marks submitted for {grade} {term}.", "success")
    return redirect(url_for("dashboard", exam_grade=grade, exam_term=term))


@app.route("/admin/institution-profile", methods=["POST"])
@login_required
@role_required("Admin")
def admin_institution_profile():
    institution_type=request.form.get("institution_type","Secondary School")
    if institution_type not in INSTITUTION_TYPES: institution_type="Secondary School"
    learner_label=request.form.get("learner_label","Student").strip() or "Student"
    staff_label=request.form.get("staff_label","Teacher").strip() or "Teacher"
    academic_period_label=request.form.get("academic_period_label","Term").strip() or "Term"
    class_label=request.form.get("class_label","Class").strip() or "Class"
    department_label=request.form.get("department_label","Department").strip() or "Department"
    parent_enabled = 0 if institution_type in {"TVET", "College", "University"} else (1 if request.form.get('parent_portal_enabled') == '1' else 0)
    execute("""UPDATE school_settings SET institution_type=?, learner_label=?, staff_label=?, academic_period_label=?, class_label=?, department_label=?, parent_portal_enabled=? WHERE id=1""",(institution_type,learner_label,staff_label,academic_period_label,class_label,department_label,parent_enabled))
    if institution_type in {"TVET","College","University"}:
        for name in DEFAULT_DEPARTMENTS:
            execute("INSERT OR IGNORE INTO departments(name,category) VALUES(?, 'Academic')",(name,))
    audit(current_user()["id"],current_user()["full_name"],"Institution Profile","Updated institution type and terminology.")
    flash("Institution profile and terminology saved.","success")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/public-settings", methods=["POST"])
@login_required
@role_required("Admin")
def admin_public_settings():
    keys=["institution_history","institution_performance","institution_religion","institution_affiliations","institution_help","institution_contact","institution_owners","developer_name","developer_about","company_name","company_about"]
    values={k:request.form.get(k,"").strip() for k in keys}
    selected=[k for k in ["institution","history","achievements","owners","developer","company"] if request.form.get(f"show_{k}")]
    execute("""UPDATE school_settings SET institution_history=?, institution_performance=?, institution_religion=?, institution_affiliations=?, institution_help=?, institution_contact=?, institution_owners=?, developer_name=?, developer_about=?, company_name=?, company_about=?, prelogin_sections=? WHERE id=1""", (values["institution_history"],values["institution_performance"],values["institution_religion"],values["institution_affiliations"],values["institution_help"],values["institution_contact"],values["institution_owners"],values["developer_name"],values["developer_about"],values["company_name"],values["company_about"],",".join(selected)))
    audit(current_user()["id"], current_user()["full_name"], "Public Information", "Pre-login information sections updated.")
    flash("Public information settings saved.", "success")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/login-access", methods=["POST"])
@login_required
@role_required("Admin")
def admin_login_access():
    actor = current_user()
    desired = request.form.get("desired_state") == "1"
    confirmed = request.form.get("confirm_login_access") == "yes"
    password = request.form.get("admin_password", "")
    if not confirmed:
        flash("Please confirm the login-access change.", "warning")
        return redirect(url_for("admin_dashboard"))
    full = q("SELECT password_hash FROM users WHERE id=? AND role='Admin' AND active=1", (actor["id"],), one=True)
    if not full or not check_password_hash(full["password_hash"], password):
        flash("Administrator password is incorrect. Login-access setting was not changed.", "danger")
        return redirect(url_for("admin_dashboard"))
    execute("UPDATE school_settings SET auth_required=? WHERE id=1", (1 if desired else 0,))
    state = "enabled" if desired else "disabled"
    audit(actor["id"], actor["full_name"], "Login Access", f"Administrator {state} the login page for non-Administrator users.")
    flash(f"Login page is now {state}.", "success")
    return redirect(url_for("admin_dashboard"))

@app.route("/users/add", methods=["POST"])
@login_required
def add_user():
    actor=current_user()
    if actor["role"] not in {"Admin","ICT"}: abort(403)
    full_name=request.form.get("full_name", "").strip()
    username=request.form.get("username", "").strip().lower()
    password=request.form.get("password", "")
    role=request.form.get("role", "Teacher")
    title=request.form.get("title", "").strip()
    department=request.form.get("department", "").strip()
    student_id=request.form.get("student_id") or None
    allowed=set(ALL_PORTAL_ROLES) - {SYSTEM_ROLE}
    # ICT is a technical operator, not a privilege escalator.
    if role not in allowed or (actor["role"]=="ICT" and role in {"Admin","ICT"}):
        flash("This account type cannot be created by your role.", "danger")
        return redirect(request.referrer or url_for("admin_dashboard"))
    if not full_name or not username or len(password)<4:
        flash("Name, username, and a password of at least 4 characters are required.", "danger")
        return redirect(request.referrer or url_for("admin_dashboard"))
    if role in {"Student","Parent"} and student_id:
        try: student_id=int(student_id)
        except ValueError: student_id=None
    else:
        student_id=None
    if q("SELECT id FROM users WHERE lower(username)=? LIMIT 1", (username,), one=True):
        flash("Username already exists. Choose a different username.", "danger")
        return redirect(request.referrer or url_for("admin_dashboard"))
    try:
        uid=execute("""INSERT INTO users(full_name, username, password_hash, role, student_id, active, title, department, phone, email, date_of_birth, gender, id_reference, address, emergency_contact, blood_group, medical_notes, accountability_notes)
                      VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                   (full_name,username,generate_password_hash(password),role,student_id,title,department,request.form.get("phone","").strip(),request.form.get("email","").strip(),request.form.get("date_of_birth","").strip(),request.form.get("gender","").strip(),request.form.get("id_reference","").strip(),request.form.get("address","").strip(),request.form.get("emergency_contact","").strip(),request.form.get("blood_group","").strip(),request.form.get("medical_notes","").strip(),request.form.get("accountability_notes","").strip()))
        if role=="Parent" and student_id:
            execute("INSERT OR IGNORE INTO guardian_links(guardian_user_id, student_id, relationship, is_primary) VALUES(?,?,?,?)", (uid,student_id,request.form.get("relationship","Guardian").strip() or "Guardian",1))
        audit(actor["id"],actor["full_name"],"Add User",f"{full_name} ({username}) added as {role}; title={title or '—'}; department={department or '—'}.")
    except sqlite3.IntegrityError:
        flash("Username already exists or the supplied learner link is invalid.", "danger")
        return redirect(request.referrer or url_for("admin_dashboard"))
    flash("Account created successfully.", "success")
    return redirect(request.referrer or url_for("admin_dashboard"))

@app.route("/users/<int:user_id>/edit", methods=["GET","POST"])
@login_required
def edit_user(user_id:int):
    actor=current_user()
    if actor["role"] not in {"Admin","ICT"}: abort(403)
    user=q("SELECT * FROM users WHERE id=?",(user_id,),one=True)
    if not user or user["role"]==SYSTEM_ROLE: abort(404)
    if actor["role"]=="ICT" and user["role"] in {"Admin","ICT"}: abort(403)
    if request.method=="POST":
        role=request.form.get("role",user["role"])
        if actor["role"]=="ICT" and role in {"Admin","ICT"}: abort(403)
        if role not in set(ALL_PORTAL_ROLES)-{SYSTEM_ROLE}: abort(400)
        student_id=request.form.get("student_id") or None
        if student_id:
            try: student_id=int(student_id)
            except ValueError: student_id=None
        else: student_id=None
        new_username = request.form.get("username","").strip().lower()
        conflict = q("SELECT id FROM users WHERE lower(username)=? AND id!=? LIMIT 1", (new_username, user_id), one=True)
        if conflict:
            flash("Username already exists. Choose a different username.", "danger")
            return redirect(url_for("edit_user", user_id=user_id))
        execute("""UPDATE users SET full_name=?, username=?, role=?, student_id=?, title=?, department=?, phone=?, email=?, date_of_birth=?, gender=?, id_reference=?, address=?, emergency_contact=?, blood_group=?, medical_notes=?, accountability_notes=? WHERE id=?""",
               (request.form.get("full_name","").strip(),new_username,role,student_id,request.form.get("title","").strip(),request.form.get("department","").strip(),request.form.get("phone","").strip(),request.form.get("email","").strip(),request.form.get("date_of_birth","").strip(),request.form.get("gender","").strip(),request.form.get("id_reference","").strip(),request.form.get("address","").strip(),request.form.get("emergency_contact","").strip(),request.form.get("blood_group","").strip(),request.form.get("medical_notes","").strip(),request.form.get("accountability_notes","").strip(),user_id))
        if role=="Parent" and student_id:
            execute("INSERT OR IGNORE INTO guardian_links(guardian_user_id,student_id,relationship,is_primary) VALUES(?,?,?,?)",(user_id,student_id,request.form.get("relationship","Guardian").strip() or "Guardian",1))
        audit(actor["id"],actor["full_name"],"Edit User",f"Updated {user['username']} ({user['role']}) -> {request.form.get('username','').strip()} ({role}).")
        flash("Person profile updated.","success")
        return redirect(url_for("admin_dashboard"))
    students=q("SELECT id, full_name, admission_no FROM students WHERE active=1 ORDER BY full_name")
    depts=q("SELECT name FROM departments WHERE active=1 ORDER BY name")
    return render_template("user_edit.html", user=user, students=students, departments=depts, role_options=ALL_PORTAL_ROLES, guardian_links=q("SELECT * FROM guardian_links WHERE guardian_user_id=? AND active=1",(user_id,)))

@app.route("/users/<int:user_id>/reset-password", methods=["POST"])
@login_required
def reset_user_password(user_id:int):
    actor=current_user()
    if actor["role"]!="Admin": abort(403)
    user=q("SELECT * FROM users WHERE id=?",(user_id,),one=True)
    if not user or user["role"]==SYSTEM_ROLE: abort(404)
    password=request.form.get("password","")
    if len(password)<4:
        flash("Temporary password must be at least 4 characters.","danger")
        return redirect(url_for("admin_dashboard"))
    execute("UPDATE users SET password_hash=? WHERE id=?",(generate_password_hash(password),user_id))
    audit(actor["id"],actor["full_name"],"Password Reset",f"Administrator reset the password for {user['username']}.")
    flash("Password reset successfully. Give the temporary password directly to the account holder.","success")
    return redirect(url_for("admin_dashboard"))

@app.route("/users/<int:user_id>/archive", methods=["POST"])
@login_required
def archive_user(user_id:int):
    actor=current_user()
    if actor["role"]!="Admin": abort(403)
    user=q("SELECT * FROM users WHERE id=?",(user_id,),one=True)
    if not user or user["role"]==SYSTEM_ROLE: abort(404)
    if user["id"]==actor["id"]:
        flash("You cannot archive your own Administrator account.","warning")
        return redirect(url_for("admin_dashboard"))
    if user["role"]=="Admin" and q("SELECT COUNT(*) AS c FROM users WHERE role='Admin' AND active=1",one=True)["c"]<=1:
        flash("The last active Administrator cannot be archived.","warning")
        return redirect(url_for("admin_dashboard"))
    execute("UPDATE users SET active=0, archived_at=CURRENT_TIMESTAMP WHERE id=?",(user_id,))
    audit(actor["id"],actor["full_name"],"Archive User",f"{user['username']} ({user['role']}) archived; historical records retained.")
    flash("Account archived. Historical records remain intact.","success")
    return redirect(url_for("admin_dashboard"))

@app.route("/users/<int:user_id>/restore", methods=["POST"])
@login_required
def restore_user(user_id:int):
    actor=current_user()
    if actor["role"]!="Admin": abort(403)
    user=q("SELECT * FROM users WHERE id=?",(user_id,),one=True)
    if not user or user["role"]==SYSTEM_ROLE: abort(404)
    execute("UPDATE users SET active=1, archived_at=NULL WHERE id=?",(user_id,))
    audit(actor["id"],actor["full_name"],"Restore User",f"{user['username']} ({user['role']}) restored.")
    flash("Account restored.","success")
    return redirect(url_for("admin_dashboard"))

@app.route("/users/<int:user_id>/guardian-link", methods=["POST"])
@login_required
def guardian_link(user_id:int):
    actor=current_user()
    if actor["role"] not in {"Admin","ICT"}: abort(403)
    user=q("SELECT * FROM users WHERE id=?",(user_id,),one=True)
    if not user or user["role"]!="Parent": abort(404)
    student_id=request.form.get("student_id")
    try: student_id=int(student_id)
    except (TypeError,ValueError): abort(400)
    execute("INSERT OR REPLACE INTO guardian_links(guardian_user_id,student_id,relationship,is_primary,notes,active) VALUES(?,?,?,?,?,1)",(user_id,student_id,request.form.get("relationship","Guardian").strip() or "Guardian",1,request.form.get("notes","").strip()))
    execute("UPDATE users SET student_id=? WHERE id=? AND student_id IS NULL",(student_id,user_id))
    audit(actor["id"],actor["full_name"],"Guardian Link",f"Linked {user['username']} to student {student_id}.")
    flash("Guardian link saved.","success")
    return redirect(url_for("admin_dashboard"))

# Backward-compatible endpoint: disable/archiving instead of destructive deletion.
@app.route("/users/<int:user_id>/delete", methods=["POST"])
@login_required
def delete_user(user_id: int):
    return archive_user(user_id)

@app.route("/export/<kind>")
@login_required
@role_required("Admin")
def export_data(kind: str):
    mapping = {
        "students": (
            "Student Export",
            [
                "admission_no", "full_name", "grade", "guardian_name", "guardian_phone", "guardian_email",
                "alt_guardian_name", "alt_guardian_phone", "alt_guardian_email", "student_phone", "student_email",
                "medical_condition", "allergies", "special_info", "notes", "payment_status", "balance", "active",
            ],
            "SELECT admission_no, full_name, grade, guardian_name, guardian_phone, guardian_email, alt_guardian_name, alt_guardian_phone, alt_guardian_email, student_phone, student_email, medical_condition, allergies, special_info, notes, payment_status, balance, active FROM students ORDER BY id",
        ),
        "users": (
            "Employee Export",
            ["full_name", "username", "role", "created_at"],
            "SELECT full_name, username, role, created_at FROM users ORDER BY id",
        ),
        "payments": (
            "Payments Export",
            ["student_id", "amount", "method", "reference_no", "recorded_by", "status", "created_at"],
            "SELECT student_id, amount, method, reference_no, recorded_by, status, created_at FROM payments ORDER BY id",
        ),
        "audit": (
            "Audit Export",
            ["actor_name", "action", "details", "created_at"],
            "SELECT actor_name, action, details, created_at FROM audit_log ORDER BY id",
        ),
    }
    if kind not in mapping:
        abort(404)
    title, headers, sql = mapping[kind]
    rows = q(sql)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([row[h] for h in headers])
    data = io.BytesIO(buffer.getvalue().encode("utf-8-sig"))
    data.seek(0)
    filename = f"{kind}_export.csv"
    audit(current_user()["id"], current_user()["full_name"], "Export Data", f"{title} downloaded.")
    return send_file(data, as_attachment=True, download_name=filename, mimetype="text/csv")



@app.route("/system-help")
@login_required
def system_help():
    user=current_user(); rows=q("SELECT * FROM system_help WHERE active=1 ORDER BY category,sort_order,title")
    visible=[]
    for row in rows:
        scope=(row["role_scope"] or "All").split(",")
        if "All" in scope or user["role"] in [x.strip() for x in scope]: visible.append(row)
    return render_template("system_help.html", role=user["role"], workspace=workspace_for(user["role"]), help_rows=visible, settings=school_settings(), actor_name=user["full_name"])

@app.route("/ai-assistant")
@login_required
def ai_assistant():
    return render_template("ai_assistant.html", role=current_user()["role"], workspace=workspace_for(current_user()["role"]), settings=school_settings(), actor_name=current_user()["full_name"])

@app.route("/ai/ask", methods=["POST"])
@login_required
def ai_ask():
    settings=school_settings(); user=current_user()
    if not int(settings["ai_enabled"] or 0): return jsonify({"error":"AI assistance is disabled by the administrator."}),403
    prompt=(request.form.get("prompt") or (request.get_json(silent=True) or {}).get("prompt") or "").strip()
    if not prompt: return jsonify({"error":"Enter a question."}),400
    if len(prompt)>8000: return jsonify({"error":"Question is too long."}),400
    provider=settings["ai_provider"] or "openai_responses"; model=settings["ai_model"] or "gpt-5.6"
    api_key=os.environ.get("OPENAI_API_KEY","").strip()
    if not api_key: return jsonify({"error":"OpenAI API is not configured on this server yet. Set OPENAI_API_KEY in the deployment environment."}),503
    system_prompt=f"You are the institutional AI assistant for {settings['school_name']}. User role: {user['role']}. Be practical, concise, safe, and never invent private institutional data. If data is not supplied in the conversation, say so."
    try:
        if provider=="openai_chat_completions":
            payload=json.dumps({"model":model,"messages":[{"role":"system","content":system_prompt},{"role":"user","content":prompt}],"temperature":0.2}).encode()
            req=urllib.request.Request("https://api.openai.com/v1/chat/completions",data=payload,headers={"Authorization":f"Bearer {api_key}","Content-Type":"application/json"},method="POST")
            with urllib.request.urlopen(req,timeout=45) as resp: data=json.loads(resp.read().decode())
            answer=data.get("choices",[{}])[0].get("message",{}).get("content","")
        else:
            payload=json.dumps({"model":model,"input":[{"role":"system","content":system_prompt},{"role":"user","content":prompt}]}).encode()
            req=urllib.request.Request("https://api.openai.com/v1/responses",data=payload,headers={"Authorization":f"Bearer {api_key}","Content-Type":"application/json"},method="POST")
            with urllib.request.urlopen(req,timeout=45) as resp: data=json.loads(resp.read().decode())
            answer=(data.get("output_text") or "").strip()
            if not answer:
                chunks=[]
                for item in data.get("output",[]) or []:
                    for content in item.get("content",[]) or []:
                        if content.get("type") in {"output_text","text"}: chunks.append(content.get("text","") or content.get("output_text",""))
                answer="\n".join(chunks).strip()
        if not answer: raise RuntimeError("AI returned an empty response.")
        execute("INSERT INTO ai_usage_log(user_id,role,provider,model,prompt_preview,response_preview,status) VALUES(?,?,?,?,?,?,?)",(user["id"],user["role"],provider,model,prompt[:300],answer[:500],"Success"))
        return jsonify({"answer":answer,"provider":provider,"model":model})
    except Exception as exc:
        execute("INSERT INTO ai_usage_log(user_id,role,provider,model,prompt_preview,response_preview,status) VALUES(?,?,?,?,?,?,?)",(user["id"],user["role"],provider,model,prompt[:300],str(exc)[:500],"Failed"))
        return jsonify({"error":f"AI request failed: {exc}"}),502

@app.route("/admin/ai-settings", methods=["POST"])
@login_required
@role_required("Admin")
def admin_ai_settings():
    enabled=1 if request.form.get("ai_enabled") else 0
    provider=request.form.get("ai_provider","openai_responses")
    if provider not in {"openai_responses","openai_chat_completions"}: provider="openai_responses"
    model=request.form.get("ai_model","gpt-5.6").strip() or "gpt-5.6"
    execute("UPDATE school_settings SET ai_enabled=?, ai_provider=?, ai_model=? WHERE id=1",(enabled,provider,model))
    audit(current_user()["id"],current_user()["full_name"],"AI Settings",f"AI {'enabled' if enabled else 'disabled'} using {provider} / {model}.")
    flash("AI configuration saved. API keys remain server-side only.","success")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/help/save", methods=["POST"])
@login_required
@role_required("Admin")
def admin_help_save():
    title=request.form.get("title","").strip(); category=request.form.get("category","Getting Started").strip(); content=request.form.get("content","").strip(); scope=request.form.get("role_scope","All").strip() or "All"
    if not title or not content: flash("Help title and content are required.","danger"); return redirect(url_for("admin_dashboard"))
    execute("INSERT INTO system_help(title,category,content,role_scope,sort_order) VALUES(?,?,?,?,?)",(title,category,content,scope,100))
    audit(current_user()["id"],current_user()["full_name"],"System Help",f"Added help article: {title}.")
    flash("System help article published.","success"); return redirect(url_for("admin_dashboard"))

@app.route("/admin/learners")
@login_required
@role_required("Admin","ICT")
def all_learners():
    rows=q("SELECT * FROM students ORDER BY active DESC,grade,full_name")
    guardians=q("""SELECT gl.student_id, GROUP_CONCAT(gu.full_name || ' (' || gl.relationship || ')', ', ') AS guardians FROM guardian_links gl JOIN users gu ON gu.id=gl.guardian_user_id WHERE gl.active=1 GROUP BY gl.student_id""")
    guardian_map={r["student_id"]:r["guardians"] for r in guardians}
    return render_template("directory.html", directory_type="Learners", rows=rows, settings=school_settings(), role=current_user()["role"], actor_name=current_user()["full_name"], guardian_map=guardian_map)

@app.route("/admin/employees")
@login_required
@role_required("Admin","ICT")
def all_employees():
    rows=q("SELECT * FROM users WHERE role NOT IN ('Student','Parent','System') ORDER BY active DESC,role,full_name")
    return render_template("directory.html", directory_type="Employees", rows=rows, settings=school_settings(), role=current_user()["role"], actor_name=current_user()["full_name"], guardian_map={})

@app.route("/users/<int:user_id>/qr")
@login_required
def user_qr(user_id:int):
    user=q("SELECT * FROM users WHERE id=? AND role!='System'",(user_id,),one=True)
    if not user: abort(404)
    if current_user()["role"] not in {"Admin","ICT"} and current_user()["id"]!=user_id: abort(403)
    token=user["qr_access_token"]
    if not token:
        token=uuid.uuid4().hex; execute("UPDATE users SET qr_access_token=? WHERE id=?",(token,user_id))
    payload=url_for("qr_landing", token=token, _external=True)
    qr=qrcode.QRCode(version=1,box_size=8,border=3); qr.add_data(payload); qr.make(fit=True)
    buf=io.BytesIO(); qr.make_image().save(buf,format="PNG"); buf.seek(0)
    return send_file(buf,mimetype="image/png",download_name=f"{secure_filename(user['username'])}-portal-qr.png")

@app.route("/qr/<token>")
def qr_landing(token:str):
    user=q("SELECT id FROM users WHERE qr_access_token=? AND active=1 AND role!='System'",(token,),one=True)
    if not user: return redirect(url_for("index"))
    # QR code intentionally opens the institution portal rather than granting passwordless login.
    return redirect(url_for("login", user=user["id"]))

@app.route("/finance/ledger", methods=["POST"])
@login_required
@role_required("Finance","Admin")
def finance_post_ledger():
    entry_type=request.form.get("entry_type","Expense"); category=request.form.get("category","General").strip() or "General"; amount=float(request.form.get("amount",0) or 0)
    description=request.form.get("description","").strip(); reference=request.form.get("reference_no","").strip(); payee=request.form.get("payee_user_id",type=int) or None
    if entry_type not in {"Income","Expense","Payroll","Adjustment"} or amount<=0 or not description:
        flash("Enter a valid ledger transaction.","danger"); return redirect(url_for("finance_dashboard"))
    if entry_type=="Payroll" and (not payee or not q("SELECT id FROM users WHERE id=? AND active=1 AND role NOT IN ('Student','Parent','System')",(payee,),one=True)):
        flash("Select a valid active employee for payroll.","danger"); return redirect(url_for("finance_dashboard"))
    execute("INSERT INTO finance_ledger(entry_type,category,payee_user_id,amount,description,reference_no,posted_by) VALUES(?,?,?,?,?,?,?)",(entry_type,category,payee,amount,description,reference,current_user()["id"]))
    audit(current_user()["id"],current_user()["full_name"],"Finance Transaction",f"Posted {entry_type} of {amount:.2f} ({reference or 'no reference'}).")
    flash("Transaction posted and locked. Only an Administrator can reverse it.","success"); return redirect(url_for("finance_dashboard"))

@app.route("/finance/ledger/<int:entry_id>/reverse", methods=["POST"])
@login_required
@role_required("Admin")
def finance_reverse_ledger(entry_id:int):
    row=q("SELECT * FROM finance_ledger WHERE id=?",(entry_id,),one=True)
    if not row: abort(404)
    if row["status"]=="Reversed": flash("Transaction is already reversed.","warning"); return redirect(url_for("finance_dashboard"))
    execute("UPDATE finance_ledger SET status='Reversed',reversed_by=?,reversed_at=CURRENT_TIMESTAMP WHERE id=? AND status='Posted'",(current_user()["id"],entry_id))
    audit(current_user()["id"],current_user()["full_name"],"Reverse Finance Transaction",f"Reversed ledger entry #{entry_id}.")
    flash("Transaction reversed by Administrator. Original record remains visible for audit.","success"); return redirect(url_for("finance_dashboard"))

@app.route("/admin/theme", methods=["POST"])
@login_required
@role_required("Admin")
def admin_theme():
    return ict_settings()


def _backup_tables():
    names=[r[0] for r in get_db().execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]
    return names

def _json_backup_payload(include_assets=True):
    conn=get_db(); tables={}
    for name in _backup_tables():
        rows=conn.execute(f"SELECT * FROM [{name}]").fetchall()
        tables[name]={"columns":list(rows[0].keys()) if rows else list(table_columns(conn,name)),"rows":[dict(r) for r in rows]}
    assets=[]
    if include_assets and UPLOAD_DIR.exists():
        for path in UPLOAD_DIR.rglob('*'):
            if not path.is_file() or path.name.endswith('.bak'): continue
            try:
                data=path.read_bytes()
                if len(data)<=30*1024*1024:
                    assets.append({"path":str(path.relative_to(DATA_DIR)).replace('\\','/'),"mime":mimetypes.guess_type(str(path))[0] or 'application/octet-stream',"data_base64":base64.b64encode(data).decode('ascii')})
            except OSError:
                continue
    settings=dict(q("SELECT * FROM school_settings WHERE id=1",one=True) or {})
    return {"format":"Prime Institution OS Full System JSON","version":2,"created_at":datetime.utcnow().isoformat(timespec='seconds')+'Z',"settings":settings,"tables":tables,"assets":assets,"notes":"Restore through Administration > Backup & Recovery. Password hashes are preserved; plaintext passwords and API keys are never exported."}

@app.route("/backup/json")
@login_required
@role_required("Admin")
def backup_json_download():
    payload=_json_backup_payload(include_assets=True)
    raw=json.dumps(payload,ensure_ascii=False,indent=2).encode('utf-8')
    name=f"prime-institution-full-backup-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
    execute("INSERT INTO backup_registry(backup_type,file_name,row_count,created_by) VALUES(?,?,?,?)",("JSON",name,sum(v.get('row_count',0) if isinstance(v,dict) else 0 for v in []),current_user()["id"]))
    return send_file(io.BytesIO(raw),mimetype='application/json',as_attachment=True,download_name=name)

@app.route("/backup/json/restore", methods=["POST"])
@login_required
@role_required("Admin")
def backup_json_restore():
    file=request.files.get('json_backup')
    if not file or not file.filename or not file.filename.lower().endswith('.json'):
        flash('Choose a .json full system backup first.','danger'); return redirect(request.referrer or url_for('admin_dashboard'))
    try: payload=json.load(file.stream)
    except Exception as exc:
        flash(f'Backup JSON could not be read: {exc}','danger'); return redirect(request.referrer or url_for('admin_dashboard'))
    if payload.get('format')!='Prime Institution OS Full System JSON' or not isinstance(payload.get('tables'),dict):
        flash('This file is not a valid Prime Institution OS backup.','danger'); return redirect(request.referrer or url_for('admin_dashboard'))
    conn=get_db(); old_autocommit=conn.isolation_level
    try:
        session['_restore_actor_username']=current_user()['username']
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute('BEGIN')
        existing=_backup_tables()
        for table in existing:
            conn.execute(f'DELETE FROM [{table}]')
        for table,data in payload['tables'].items():
            if table.startswith('sqlite_') or table not in existing: continue
            columns=data.get('columns') or []
            for row in data.get('rows') or []:
                cols=[c for c in columns if c in table_columns(conn,table)]
                if not cols: continue
                vals=[row.get(c) for c in cols]
                placeholders=','.join('?' for _ in cols)
                conn.execute(f"INSERT INTO [{table}] ({','.join('['+c+']' for c in cols)}) VALUES ({placeholders})",vals)
        for asset in payload.get('assets') or []:
            rel=str(asset.get('path',''))
            if not rel.startswith('uploads/'): continue
            target=(DATA_DIR/rel).resolve(); root=UPLOAD_DIR.resolve()
            if root not in target.parents: continue
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(base64.b64decode(asset.get('data_base64','')))
        conn.commit()
        conn.execute('PRAGMA foreign_keys=ON')
        init_db()
        restored_actor=q('SELECT id,full_name FROM users WHERE username=? AND active=1 LIMIT 1',(session.get('_restore_actor_username',''),),one=True)
        if not restored_actor:
            restored_actor=q("SELECT id,full_name FROM users WHERE role='Admin' AND active=1 ORDER BY id LIMIT 1",one=True)
        if restored_actor:
            audit(restored_actor['id'],restored_actor['full_name'],'Restore JSON Backup',f"Full system JSON backup restored: {file.filename}.")
        flash('Full system JSON restored successfully. Database records, settings and included uploaded assets are back in place.','success')
    except Exception as exc:
        conn.rollback(); conn.execute('PRAGMA foreign_keys=ON'); flash(f'JSON restore failed safely: {exc}','danger')
    finally:
        conn.isolation_level=old_autocommit
    return redirect(request.referrer or url_for('admin_dashboard'))

@app.route("/backup/download")
@login_required
@role_required("Admin")
def backup_download():
    if not DB_PATH.exists():
        abort(404)
    return send_file(DB_PATH, as_attachment=True, download_name="school_backup.sqlite3")


@app.route("/backup/restore", methods=["POST"])
@login_required
@role_required("Admin")
def backup_restore():
    file = request.files.get("backup_file")
    if not file or not file.filename:
        flash("Choose a database backup file first.", "danger")
        return redirect(request.referrer or url_for("admin_dashboard"))
    if not allowed_filename(file.filename):
        flash("Only .db, .sqlite, or .sqlite3 files are allowed.", "danger")
        return redirect(request.referrer or url_for("admin_dashboard"))
    safe_name = secure_filename(file.filename)
    temp_path = UPLOAD_DIR / safe_name
    file.save(temp_path)
    old_db=None
    try:
        with sqlite3.connect(temp_path) as test_conn:
            integrity=(test_conn.execute("PRAGMA integrity_check").fetchone() or [""])[0]
            required={"users","students","school_settings","payments","exam_batches","exam_results"}
            present={r[0] for r in test_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if integrity != "ok" or not required.issubset(present):
                raise ValueError("The selected file is not a valid School Portal backup.")
        backup_old = DB_PATH.with_suffix(".bak")
        if DB_PATH.exists():
            if backup_old.exists(): backup_old.unlink()
            DB_PATH.replace(backup_old)
            old_db=backup_old
        temp_path.replace(DB_PATH)
        try:
            init_db()
        except Exception:
            if DB_PATH.exists(): DB_PATH.unlink()
            if old_db and old_db.exists(): old_db.replace(DB_PATH)
            init_db()
            raise
        flash("Backup restored successfully. The previous database was retained as a rollback copy.", "success")
        audit(current_user()["id"], current_user()["full_name"], "Restore Backup", f"Backup restored from {safe_name}.")
    except Exception as exc:
        if temp_path.exists(): temp_path.unlink(missing_ok=True)
        flash(f"Restore failed safely: {exc}", "danger")
    return redirect(request.referrer or url_for("admin_dashboard"))


@app.route("/uploads/<path:filename>")
def uploaded_file(filename: str):
    # Public assets (logo, library, election and institution media) can be served.
    # Private finance/document artifacts must never be anonymously retrievable.
    rel_parts = [secure_filename(part) for part in Path(filename).parts if part not in {"", ".", ".."}]
    relative_name = "/".join(rel_parts)
    first_part = rel_parts[0] if rel_parts else ""
    private = first_part in {"documents", "qr", "receipts"} or (len(rel_parts) == 1 and rel_parts[0].startswith("receipt-"))
    if private and not current_user():
        abort(403)
    if private and current_user()["role"] in {"Student", "Parent"}:
        abort(403)
    if not rel_parts:
        abort(404)
    target = (UPLOAD_DIR / Path(*rel_parts)).resolve()
    upload_root = UPLOAD_DIR.resolve()
    if upload_root not in target.parents and target != upload_root:
        abort(403)
    if not target.exists() or not target.is_file():
        abort(404)
    return send_file(target)


@app.route("/api/student/<int:student_id>")
@login_required
def api_student(student_id: int):
    student = q("SELECT * FROM students WHERE id = ?", (student_id,), one=True)
    if not student:
        return jsonify({"error": "Not found"}), 404
    if not can_access_student(student_id):
        return jsonify({"error": "Forbidden"}), 403
    payments = q(
        """
        SELECT p.*, u.full_name AS recorded_by_name
        FROM payments p
        JOIN users u ON u.id = p.recorded_by
        WHERE p.student_id = ?
        ORDER BY p.created_at DESC, p.id DESC
        """,
        (student_id,),
    )
    return jsonify({"student": dict(student), "payments": [dict(p) for p in payments]})


@app.errorhandler(403)
def forbidden(_):
    return render_template("error.html", title="Access denied", message="You do not have permission to access this area."), 403


@app.errorhandler(404)
def not_found(_):
    return render_template("error.html", title="Not found", message="The page you requested could not be found."), 404


@app.errorhandler(413)
def too_large(_):
    return render_template("error.html", title="File too large", message="The uploaded file is too large."), 413


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
