from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path


def connect() -> sqlite3.Connection:
    path = Path(os.getenv("ASSISTANT_DB", "work/assistant.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS employees(
      email TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('employee','manager','admin')),
      telegram_id TEXT UNIQUE, binding_confirmed INTEGER NOT NULL DEFAULT 0,
      manager_email TEXT REFERENCES employees(email), active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS mailboxes(
      mailbox TEXT PRIMARY KEY, connected_at TEXT NOT NULL, cursor TEXT,
      last_sync TEXT, last_error TEXT, manager_email TEXT REFERENCES employees(email)
    );
    CREATE TABLE IF NOT EXISTS tasks(
      id INTEGER PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, mailbox TEXT NOT NULL,
      message_id TEXT NOT NULL, conversation_id TEXT, title TEXT NOT NULL, description TEXT NOT NULL,
      sender TEXT NOT NULL, received_at TEXT NOT NULL, assignee_email TEXT REFERENCES employees(email),
      proposed_assignee_email TEXT REFERENCES employees(email),
      due_at TEXT, due_precision TEXT NOT NULL DEFAULT 'unknown', priority TEXT NOT NULL DEFAULT 'normal',
      status TEXT NOT NULL DEFAULT 'Новая', confidence REAL NOT NULL DEFAULT 0,
      needs_review INTEGER NOT NULL DEFAULT 1, sensitive INTEGER NOT NULL DEFAULT 0,
      source_url TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      attachments TEXT NOT NULL DEFAULT '[]'
    );
    CREATE TABLE IF NOT EXISTS audit(
      id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
      task_id INTEGER REFERENCES tasks(id), metadata TEXT NOT NULL DEFAULT '{}',
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS outbox(
      id INTEGER PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE, recipient TEXT NOT NULL,
      kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
      attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS binding_codes(
      code_hash TEXT PRIMARY KEY, email TEXT NOT NULL REFERENCES employees(email),
      expires_at TEXT NOT NULL, used_at TEXT
    );
    CREATE TABLE IF NOT EXISTS bot_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    # Lightweight forward-compatible migration for databases created by earlier scaffold versions.
    mailbox_cols={r[1] for r in db.execute("PRAGMA table_info(mailboxes)")}
    if "manager_email" not in mailbox_cols:
        db.execute("ALTER TABLE mailboxes ADD COLUMN manager_email TEXT REFERENCES employees(email)")
    task_cols={r[1] for r in db.execute("PRAGMA table_info(tasks)")}
    if "proposed_assignee_email" not in task_cols:
        db.execute("ALTER TABLE tasks ADD COLUMN proposed_assignee_email TEXT REFERENCES employees(email)")
    if "attachments" not in task_cols:
        db.execute("ALTER TABLE tasks ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'")
    db.commit()


def audit(db: sqlite3.Connection, actor: str, action: str, task_id: int | None = None, metadata: dict | None = None) -> None:
    db.execute("INSERT INTO audit(actor,action,task_id,metadata) VALUES(?,?,?,?)",
               (actor, action, task_id, json.dumps(metadata or {}, ensure_ascii=False)))
