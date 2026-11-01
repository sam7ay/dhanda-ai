import json
import os
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

# Vercel functions can only write to /tmp, so the databases there reset when the function restarts.
BASE_DIR = Path("/tmp/dhanda") if os.environ.get("VERCEL") else Path(__file__).resolve().parent.parent / "data"

# Every logged-in business gets its own SQLite file; the request middleware picks which one.
_tenant = ContextVar("tenant", default="default")


def use_tenant(name):
    _tenant.set(name)


def tenant():
    return _tenant.get()


def db_path():
    return BASE_DIR / f"{_tenant.get()}.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS business (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL,
    owner_name TEXT NOT NULL,
    owner_mobile TEXT NOT NULL,
    city TEXT NOT NULL,
    erp TEXT NOT NULL,
    test_numbers TEXT NOT NULL DEFAULT '[]',
    data_source TEXT NOT NULL DEFAULT 'sample',
    imported_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    mobile TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS otps (
    mobile TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS retailers (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    owner_name TEXT NOT NULL,
    shop_name TEXT NOT NULL,
    city TEXT NOT NULL,
    phone TEXT NOT NULL,
    language TEXT NOT NULL,
    credit_limit REAL NOT NULL,
    segment TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS skus (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    name_hi TEXT NOT NULL,
    category TEXT NOT NULL,
    unit TEXT NOT NULL,
    price REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY,
    number TEXT NOT NULL,
    retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    amount REAL NOT NULL,
    paid REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS invoice_lines (
    id INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    sku_id INTEGER NOT NULL REFERENCES skus(id),
    qty INTEGER NOT NULL,
    rate REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY,
    retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    date TEXT NOT NULL,
    amount REAL NOT NULL,
    mode TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    upsell INTEGER NOT NULL DEFAULT 1,
    sarvam_campaign_id TEXT,
    sarvam_cohort_id TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    campaign_id INTEGER REFERENCES campaigns(id),
    retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    dialed_number TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    variables TEXT NOT NULL DEFAULT '{}',
    sarvam_attempt_id TEXT,
    connectivity TEXT,
    duration REAL,
    language TEXT,
    outcome TEXT,
    promised_amount REAL,
    promised_date TEXT,
    upsell_interest TEXT,
    products_interested TEXT,
    transferred INTEGER NOT NULL DEFAULT 0,
    transcript TEXT,
    summary TEXT
);
"""


def connect():
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    conn = sqlite3.connect(path)
    if fresh:  # the file can disappear under a running server (e.g. /tmp cleanup on Vercel)
        conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    with tx() as conn:
        conn.executescript(SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(business)")}
        if "data_source" not in cols:
            conn.execute("ALTER TABLE business ADD COLUMN data_source TEXT NOT NULL DEFAULT 'sample'")
            conn.execute("ALTER TABLE business ADD COLUMN imported_at TEXT")


def rows(sql, params=()):
    with tx() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def row(sql, params=()):
    found = rows(sql, params)
    return found[0] if found else None


def execute(sql, params=()):
    with tx() as conn:
        return conn.execute(sql, params).lastrowid


def business():
    b = row("SELECT * FROM business WHERE id = 1")
    if b:
        # Test contacts: [{"name", "mobile"}]. Older rows stored bare numbers.
        b["test_numbers"] = [c if isinstance(c, dict) else {"name": "", "mobile": c}
                             for c in json.loads(b["test_numbers"])]
    return b
