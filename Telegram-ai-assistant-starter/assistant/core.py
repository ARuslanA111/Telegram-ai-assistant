from __future__ import annotations

import hashlib
import json
import sqlite3
import os
from datetime import datetime, timezone

STATUSES = ("Новая", "Принята", "В работе", "Ждёт ответа/материалов", "На проверке", "Выполнена", "Отменена")


def ingest(db: sqlite3.Connection, mailbox: str, message: dict, extraction: dict) -> int | None:
    """Store only validated extraction fields. Raw email body must not enter logs/database."""
    if not isinstance(extraction,dict) or not isinstance(extraction.get("is_task"),bool):
        raise ValueError("Extraction does not match task schema")
    msgid = str(message["id"])
    key = hashlib.sha256(f"{mailbox}\0{msgid}".encode()).hexdigest()
    existing = db.execute("SELECT id FROM tasks WHERE source_key=?", (key,)).fetchone()
    if existing:
        return int(existing[0])
    title=str(extraction.get("title") or "Требует уточнения").strip()[:240]
    description=str(extraction.get("description") or "").strip()[:4000]
    try: confidence=float(extraction.get("confidence",0))
    except (TypeError,ValueError): confidence=0
    if not 0 <= confidence <= 1: confidence=0
    due_at=extraction.get("due_at")
    due_precision=str(extraction.get("due_precision","unknown"))
    if due_at:
        try:
            datetime.fromisoformat(str(due_at).replace("Z","+00:00"))
        except ValueError:
            try: datetime.fromisoformat(str(due_at)+"T00:00:00")
            except ValueError: due_at=None; due_precision="unknown"
    else:
        due_at=None; due_precision="unknown"
    priority=extraction.get("priority","normal")
    if priority not in ("low","normal","high"): priority="normal"
    assignee = extraction.get("assignee_email")
    if isinstance(assignee,str): assignee=assignee.strip().lower()
    else: assignee=None
    matched = db.execute("SELECT email FROM employees WHERE email=? AND active=1", (assignee,)).fetchone() if assignee else None
    if not extraction["is_task"] and confidence >= 0.75:
        return None
    if not extraction["is_task"] and confidence < 0.3:
        return None
    unclear = not matched or confidence < 0.75 or extraction.get("needs_clarification") is True or not title or not extraction["is_task"]
    manager_approval = os.getenv("REQUIRE_MANAGER_APPROVAL", "true").lower() == "true"
    needs_review = unclear or bool(extraction.get("sensitive")) or manager_approval
    conversation=message.get("conversation_id")
    if conversation and title:
        duplicate=db.execute("SELECT id FROM tasks WHERE conversation_id=? AND lower(title)=lower(?) AND status NOT IN ('Выполнена','Отменена') LIMIT 1",
                             (conversation,title)).fetchone()
        if duplicate: return int(duplicate[0])
    status = "На проверке" if needs_review else "Новая"
    cur = db.execute("""INSERT INTO tasks(source_key,mailbox,message_id,conversation_id,title,description,sender,
      received_at,assignee_email,proposed_assignee_email,due_at,due_precision,priority,status,confidence,needs_review,sensitive,source_url,attachments)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      (key, mailbox, msgid, conversation, title,
       description, str(message.get("sender", ""))[:320],
       str(message.get("received_at", datetime.now(timezone.utc).isoformat())), matched[0] if matched and not needs_review else None,
       matched[0] if matched and needs_review else None,
       due_at, due_precision, priority,
       status, confidence, int(needs_review), int(bool(extraction.get("sensitive"))),
       message.get("source_url"), json.dumps(message.get("attachments",[]),ensure_ascii=False)))
    tid = int(cur.lastrowid)
    db.execute("INSERT INTO audit(actor,action,task_id,metadata) VALUES(?,?,?,?)", ("system", "task_created", tid, json.dumps({"review": needs_review})))
    if matched and not needs_review:
        db.execute("INSERT OR IGNORE INTO outbox(dedupe_key,recipient,kind,payload) VALUES(?,?,?,?)",
          (f"task:{tid}:new", matched[0], "new_task", json.dumps({"task_id": tid})))
    elif needs_review:
        manager=db.execute("SELECT manager_email FROM mailboxes WHERE mailbox=?",(mailbox,)).fetchone()
        if manager and manager[0]:
            db.execute("INSERT OR IGNORE INTO outbox(dedupe_key,recipient,kind,payload) VALUES(?,?,?,?)",
              (f"task:{tid}:review",manager[0],"review_task",json.dumps({"task_id":tid})))
    db.commit()
    return tid


def employee_for_telegram(db: sqlite3.Connection, telegram_id: str):
    return db.execute("SELECT * FROM employees WHERE telegram_id=? AND binding_confirmed=1 AND active=1", (telegram_id,)).fetchone()


def tasks_for(db: sqlite3.Connection, telegram_id: str):
    user = employee_for_telegram(db, telegram_id)
    if not user:
        raise PermissionError("Требуется подтверждённая привязка Telegram ID")
    return db.execute("SELECT * FROM tasks WHERE assignee_email=? ORDER BY due_at IS NULL,due_at", (user["email"],)).fetchall()


def report(db: sqlite3.Connection, telegram_id: str, start: str, end: str):
    user = employee_for_telegram(db, telegram_id)
    if not user or user["role"] not in ("manager", "admin"):
        raise PermissionError("Отчёт доступен только подтверждённому руководителю")
    if user["role"] == "admin":
        return db.execute("SELECT * FROM tasks WHERE received_at>=? AND received_at<? ORDER BY assignee_email,due_at", (start, end)).fetchall()
    return db.execute("""SELECT t.* FROM tasks t LEFT JOIN mailboxes b ON b.mailbox=t.mailbox
      WHERE t.received_at>=? AND t.received_at<? AND (
        t.assignee_email=? OR t.assignee_email IN (SELECT email FROM employees WHERE manager_email=?)
        OR b.manager_email=?) ORDER BY t.assignee_email,t.due_at""",
      (start,end,user["email"],user["email"],user["email"])).fetchall()


def change_status(db: sqlite3.Connection, telegram_id: str, task_id: int, status: str) -> None:
    user = employee_for_telegram(db, telegram_id)
    if not user or status not in STATUSES:
        raise PermissionError("Недопустимая операция или отсутствует подтверждённая привязка")
    cur = db.execute("UPDATE tasks SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND assignee_email=?", (status, task_id, user["email"]))
    if not cur.rowcount:
        raise PermissionError("Задача недоступна")
    db.execute("INSERT INTO audit(actor,action,task_id,metadata) VALUES(?,?,?,?)", (user["email"], "status_changed", task_id, json.dumps({"status": status}, ensure_ascii=False)))
    db.commit()
