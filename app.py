from __future__ import annotations

import csv
import json
import io
import os
import sqlite3
import uuid
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
from werkzeug.utils import secure_filename
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
import qrcode
from PIL import Image
from pypdf import PdfReader, PdfWriter

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
DB_PATH = INSTANCE_DIR / "school.db"
UPLOAD_DIR = BASE_DIR / "uploads"
ALLOWED_RESTORE_EXT = {"db", "sqlite", "sqlite3"}
PUBLIC_ROLES = ("Teacher", "Student", "Parent")
HIDDEN_ROLES = ("Admin", "ICT", "Finance", "Librarian")
ALL_PORTAL_ROLES = HIDDEN_ROLES + PUBLIC_ROLES
ADMIN_LOGIN_PATH = "/xtspolsjhulupjoppsup-lmkzcodup"
ADMIN_ROLES = {"Admin"}
ALL_ROLES = ALL_PORTAL_ROLES
SYSTEM_ROLE = "System"
_PORTAL_ROLE_COOKIE = "school_portal_role"

app = Flask(__name__, instance_path=str(INSTANCE_DIR), instance_relative_config=True)
app.config.update(
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,
    TEMPLATES_AUTO_RELOAD=True,
)

# -------------------------------------------------------------------
# Signed authentication session
# -------------------------------------------------------------------
SECRET_FILE = INSTANCE_DIR / "secret.key"
if os.environ.get("SECRET_KEY"):
    _secret_key = os.environ["SECRET_KEY"]
elif SECRET_FILE.exists():
    _secret_key = SECRET_FILE.read_text().strip()
else:
    _secret_key = uuid.uuid4().hex + uuid.uuid4().hex
    INSTANCE_DIR.mkdir(exist_ok=True)
    SECRET_FILE.write_text(_secret_key)

app.config.update(
    SECRET_KEY=_secret_key,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("COOKIE_SECURE") == "1" or os.environ.get("RENDER")),
    SESSION_COOKIE_NAME="school_portal_session",
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
    with sqlite3.connect(DB_PATH) as conn:
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
        ensure_column(conn, "school_settings", "auth_initialized INTEGER NOT NULL DEFAULT 0")

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
@app.before_request
def load_current_user() -> None:
    g.user = None
    user_id = session.get("user_id")
    if user_id:
        g.user = q("SELECT id, full_name, username, role, student_id, active FROM users WHERE id = ? AND active = 1 AND role != 'System'", (user_id,), one=True)


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


@app.context_processor
def auth_template_context():
    return {"current_user": current_user(), "all_roles": ALL_PORTAL_ROLES, "public_roles": PUBLIC_ROLES}


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
    }


# -------------------------
# Routes
# -------------------------
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
    if role and role != "Admin" and not auth_required():
        return enter_role_without_login(role)
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = selected_role_from_request()
        user = q("SELECT * FROM users WHERE username=? AND active=1 AND role=?", (username, role), one=True)
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Invalid username, password, or role.", "danger")
            return render_template("login.html", portal_title=settings["school_name"], school_settings=settings, theme_color=settings["primary_color"], login_role=role, error="Invalid username, password, or role.", setup_required=not auth_initialized())
        session.clear()
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
    return redirect(url_for("index"))


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
    users = q("SELECT id, full_name, username, role, created_at FROM users ORDER BY created_at DESC")
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
    items=q("SELECT * FROM library_items WHERE active=1 ORDER BY category,title")
    loans=q("""SELECT l.*, i.title, s.full_name AS student_name, s.admission_no FROM library_loans l JOIN library_items i ON i.id=l.item_id JOIN students s ON s.id=l.student_id WHERE l.status='Issued' ORDER BY l.due_date, l.issued_at""")
    return render_template("library.html", items=items, loans=loans, settings=school_settings(), actor_name=current_user()["full_name"])

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
    nav_items=navigation_items("Finance", settings)
    return render_template("role_dashboard.html", role="Finance", workspace=workspace_for("Finance"), settings=settings, payments=payments, total_income=total_income, total_balance=balance, students=students, batches=batches, documents=documents, actor_name=current_user()["full_name"], nav_items=nav_items)


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
    menu_order=request.form.get("menu_order", "Home,Assignments,Submissions,Online classes").strip() or "Home,Assignments,Submissions,Online classes"
    labels={k: request.form.get(k, defaults).strip() or defaults for k,defaults in [("home_label","Home"),("assignments_label","Assignments"),("results_label","Results"),("messages_label","Messages"),("finance_label","Finance"),("branding_label","Branding")]}
    execute("""UPDATE school_settings SET school_name=?, portal_subtitle=?, primary_color=?, accent_color=?, background_color=?, panel_color=?, menu_order=?, home_label=?, assignments_label=?, results_label=?, messages_label=?, finance_label=?, branding_label=? WHERE id=1""", (school_name,portal_subtitle,primary,accent,bg,panel,menu_order,labels["home_label"],labels["assignments_label"],labels["results_label"],labels["messages_label"],labels["finance_label"],labels["branding_label"]))
    audit(current_user()["id"], current_user()["full_name"], "ICT Branding Update", f"Portal branding/customization updated for {school_name}.")
    flash("ICT customization saved. All dashboards now use the new portal identity and theme.", "success")
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
    title=request.form.get("title","").strip(); category=request.form.get("category","Book").strip() or "Book"; author=request.form.get("author","").strip(); code=request.form.get("item_code","").strip(); location=request.form.get("location","").strip(); resource_type=request.form.get("resource_type","Physical").strip(); qty=max(1,request.form.get("quantity",type=int) or 1); description=request.form.get("description","").strip(); external_url=request.form.get("external_url","").strip()
    if not title: flash("Library title is required.","danger"); return redirect(request.referrer or url_for("librarian_dashboard"))
    file=request.files.get("resource"); file_path=""
    if file and file.filename:
        fname=secure_filename(file.filename); dest=UPLOAD_DIR/"library"; dest.mkdir(exist_ok=True); out=dest/f"{uuid.uuid4().hex}-{fname}"; file.save(out); file_path="uploads/library/"+out.name; resource_type="Digital"
    execute("INSERT INTO library_items(title,category,author,item_code,quantity,available_quantity,location,resource_type,file_path,external_url,description,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(title,category,author,code,qty,qty,location,resource_type,file_path,external_url,description,current_user()["id"]))
    flash("Library item added.","success"); return redirect((url_for("ict_dashboard") if current_user()["role"]=="ICT" else url_for("librarian_dashboard") if current_user()["role"]=="Librarian" else url_for("admin_dashboard"))+"#library")

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
    full_name = request.form.get("full_name", "").strip()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "Teacher")
    student_id = request.form.get("student_id") or None
    allowed = set(ALL_PORTAL_ROLES) - {SYSTEM_ROLE}
    if role not in allowed or (actor["role"] == "ICT" and role == "Admin"):
        flash("This account type cannot be created by your role.", "danger")
        return redirect(request.referrer or url_for("admin_dashboard"))
    if not full_name or not username or len(password) < 4:
        flash("Name, username, and a password of at least 4 characters is required.", "danger")
        return redirect(request.referrer or url_for("admin_dashboard"))
    if role in {"Student","Parent"} and student_id:
        try: student_id=int(student_id)
        except ValueError: student_id=None
    else:
        student_id=None
    try:
        execute("INSERT INTO users(full_name, username, password_hash, role, student_id, active) VALUES (?, ?, ?, ?, ?, 1)", (full_name, username, generate_password_hash(password), role, student_id))
        audit(actor["id"], actor["full_name"], "Add User", f"{full_name} ({username}) added as {role}.")
    except sqlite3.IntegrityError:
        flash("Username already exists or student link is invalid.", "danger")
        return redirect(request.referrer or url_for("admin_dashboard"))
    flash("User created.", "success")
    return redirect(request.referrer or url_for("admin_dashboard"))


@app.route("/users/<int:user_id>/delete", methods=["POST"])
@login_required
def delete_user(user_id: int):
    actor=current_user()
    if actor["role"] != "Admin": abort(403)
    user = q("SELECT * FROM users WHERE id = ?", (user_id,), one=True)
    if not user or user["role"] == "System": abort(404)
    if user["id"] == actor["id"]:
        flash("You cannot delete your own account.", "warning")
        return redirect(request.referrer or url_for("admin_dashboard"))
    if user["role"] == "Admin" and q("SELECT COUNT(*) AS c FROM users WHERE role='Admin' AND active=1", one=True)["c"] <= 1:
        flash("The last active Administrator cannot be disabled.", "warning")
        return redirect(request.referrer or url_for("admin_dashboard"))
    # Keep existing audit/payment records intact; deactivate the account instead of hard deleting it.
    execute("UPDATE users SET active=0 WHERE id=?", (user_id,))
    audit(actor["id"], actor["full_name"], "Disable User", f"{user['username']} ({user['role']}) disabled.")
    flash("User disabled.", "success")
    return redirect(request.referrer or url_for("admin_dashboard"))


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
