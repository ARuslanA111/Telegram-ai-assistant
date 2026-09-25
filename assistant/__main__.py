from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import threading
from datetime import datetime, timezone

from .core import ingest, report, tasks_for
from .db import connect, init


def load_env():
    path=os.getenv("ENV_FILE", ".env")
    try:
        for line in open(path,encoding="utf-8"):
            line=line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k,v=line.split("=",1); os.environ.setdefault(k.strip(),v.strip().strip('"').strip("'"))
    except FileNotFoundError: pass


def demo(db):
    now = datetime.now(timezone.utc).isoformat()
    db.execute("INSERT OR IGNORE INTO employees(email,role,telegram_id,binding_confirmed) VALUES(?,?,?,?)",
               ("demo.employee@example.invalid", "employee", "demo-telegram-id", 1))
    db.execute("INSERT OR IGNORE INTO employees(email,role,telegram_id,binding_confirmed) VALUES(?,?,?,?)",
               ("demo.manager@example.invalid", "manager", "demo-manager-id", 1))
    db.execute("INSERT OR IGNORE INTO mailboxes(mailbox,connected_at) VALUES(?,?)", ("demo@example.invalid", now))
    message = {"id": "demo-message-001", "conversation_id": "demo-conversation", "sender": "demo.manager@example.invalid",
               "received_at": now, "source_url": "https://example.invalid/demo-message"}
    extraction = {"is_task": True, "title": "Подготовить демонстрационную сводку", "description": "Пример записи; никаких реальных писем не читали.",
                  "assignee_email": "demo.employee@example.invalid", "due_precision": "unknown", "priority": "normal", "confidence": 0.99}
    task_id = ingest(db, "demo@example.invalid", message, extraction)
    print(f"Демо готово: задача #{task_id}; внешние сервисы не вызывались.")


def main():
    parser = argparse.ArgumentParser(prog="assistant", description="Локальный каркас ассистента отдела")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="создать локальную базу")
    sub.add_parser("demo", help="добавить синтетические демонстрационные данные")
    sub.add_parser("status", help="показать состояние локальной базы и ящиков")
    p = sub.add_parser("today", help="показать задачи сотрудника (нужен подтверждённый Telegram ID)")
    p.add_argument("telegram_id")
    p = sub.add_parser("report", help="отчёт руководителю за даты ISO")
    p.add_argument("telegram_id"); p.add_argument("start", help="включительная дата/время ISO")
    p.add_argument("end", help="исключительная дата/время ISO")
    p = sub.add_parser("sync", help="выполнить один безопасный проход синхронизации")
    p = sub.add_parser("add-user", help="добавить сотрудника в справочник")
    p.add_argument("email"); p.add_argument("role",choices=["employee","manager","admin"])
    p.add_argument("--manager-email", help="подтверждённый руководитель этого сотрудника")
    p = sub.add_parser("add-mailbox", help="подключить ящик в реестр синхронизации")
    p.add_argument("email")
    p.add_argument("--manager-email", help="руководитель, отвечающий за неразобранные письма ящика")
    p = sub.add_parser("bind-code", help="выдать одноразовый код подтверждённой привязки")
    p.add_argument("email")
    sub.add_parser("run", help="запустить Telegram polling и плановую синхронизацию")
    args = parser.parse_args()
    load_env()
    if os.getenv("APP_MODE", "demo").lower() == "live" and args.command not in ("init","add-user","add-mailbox","bind-code","status","run","sync"):
        parser.error("APP_MODE=live недоступен: Graph/Telegram/OneDrive адаптеры ещё не подключены")
    db = connect(); init(db)
    if args.command == "init":
        print("Локальная база готова.")
    elif args.command == "demo":
        demo(db)
    elif args.command == "status":
        boxes = [dict(row) for row in db.execute("SELECT mailbox,connected_at,last_sync,last_error FROM mailboxes")]
        print(json.dumps({"mode": os.getenv("APP_MODE", "demo"), "mailboxes": boxes, "tasks": db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                          "review_queue": db.execute("SELECT COUNT(*) FROM tasks WHERE needs_review=1").fetchone()[0],
                          "outbox_queued": db.execute("SELECT COUNT(*) FROM outbox WHERE state='queued'").fetchone()[0]}, ensure_ascii=False, indent=2))
    elif args.command == "today":
        try:
            rows = tasks_for(db, args.telegram_id)
            print("\n".join(f"#{r['id']} [{r['status']}] {r['title']} — срок: {r['due_at'] or 'не указан'}" for r in rows) or "Открытых задач нет.")
        except PermissionError as e:
            print(str(e), file=sys.stderr); return 2
    elif args.command == "report":
        try:
            rows = report(db, args.telegram_id, args.start, args.end)
            print("\n".join(f"#{r['id']} {r['assignee_email'] or 'без исполнителя'} [{r['status']}] {r['title']}" for r in rows) or "Записей за период нет.")
        except PermissionError as e:
            print(str(e), file=sys.stderr); return 2
    elif args.command == "sync":
        from .worker import sync_once
        sync_once()
    elif args.command == "add-user":
        db.execute("INSERT INTO employees(email,role,manager_email) VALUES(?,?,?) ON CONFLICT(email) DO UPDATE SET role=excluded.role,manager_email=excluded.manager_email,active=1",(args.email.lower(),args.role,args.manager_email.lower() if args.manager_email else None)); db.commit()
        print("Пользователь добавлен без Telegram-привязки.")
    elif args.command == "add-mailbox":
        db.execute("INSERT INTO mailboxes(mailbox,connected_at,manager_email) VALUES(?,?,?) ON CONFLICT(mailbox) DO UPDATE SET manager_email=excluded.manager_email",(args.email.lower(),datetime.now(timezone.utc).isoformat(),args.manager_email.lower() if args.manager_email else None)); db.commit()
        print("Ящик добавлен. Первый delta-запрос ограничится временем подключения.")
    elif args.command == "bind-code":
        code=secrets.token_urlsafe(8)
        email=args.email.lower()
        if not db.execute("SELECT 1 FROM employees WHERE email=? AND active=1",(email,)).fetchone(): parser.error("Сначала добавьте email командой add-user")
        expires=datetime.fromtimestamp(datetime.now(timezone.utc).timestamp()+900,timezone.utc).isoformat()
        db.execute("INSERT INTO binding_codes(code_hash,email,expires_at) VALUES(?,?,?)",(hashlib.sha256(code.encode()).hexdigest(),email,expires)); db.commit()
        print(f"Передайте код сотруднику по подтверждённому каналу; действует 15 минут: {code}")
    elif args.command == "run":
        if os.getenv("APP_MODE","demo").lower()=="demo":
            print("Demo mode: сетевые сервисы выключены. Для Telegram задайте APP_MODE=live и TELEGRAM_BOT_TOKEN в локальном .env.")
            return 0
        from .bot import run_bot
        from .worker import schedule_loop
        threading.Thread(target=schedule_loop,daemon=True).start()
        run_bot()
    return 0


if __name__ == "__main__":
    raise SystemExit(main() or 0)
