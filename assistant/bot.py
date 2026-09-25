from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .core import STATUSES, change_status, employee_for_telegram, report, tasks_for
from .db import audit, connect, init
from .integrations import Extractor, Telegram


def send_task(bot, chat, task):
    deadline = task["due_at"] or "не указан"
    source = task["source_url"] or "ссылка недоступна"
    attachments=json.loads(task["attachments"] or "[]") if "attachments" in task.keys() else []
    names="\n".join(f"• {a['name']}"+("" if a.get("allowed_by_policy") else " (формат/размер не разрешён)") for a in attachments[:5])
    text = f"Задача #{task['id']}: {task['title']}\nСрок: {deadline}\nПриоритет: {task['priority']}\nИсточник: {task['sender']}\n{source}"
    if names: text+="\nВложения (только метаданные):\n"+names
    keyboard = [[{"text":"Принять","callback_data":f"accept:{task['id']}"}, {"text":"Уточнить","callback_data":f"clarify:{task['id']}"}, {"text":"Не моя задача","callback_data":f"wrong:{task['id']}"}]]
    bot.send(chat,text,keyboard)


def linked_user(db, tg_id):
    user=employee_for_telegram(db,str(tg_id))
    if not user: return None
    return user


def manager_owns_task(db, user, task):
    if user["role"] == "admin": return True
    if user["role"] != "manager": return False
    if task["assignee_email"] == user["email"] or task["proposed_assignee_email"] == user["email"]: return True
    member=db.execute("SELECT 1 FROM employees WHERE email IN (?,?) AND manager_email=?",
                      (task["assignee_email"],task["proposed_assignee_email"],user["email"])).fetchone()
    mailbox=db.execute("SELECT manager_email FROM mailboxes WHERE mailbox=?",(task["mailbox"],)).fetchone()
    return bool(member or (mailbox and mailbox[0]==user["email"]))


def manager_owns_employee(db,user,email):
    if user["role"]=="admin": return True
    if user["role"]!="manager": return False
    return bool(db.execute("SELECT 1 FROM employees WHERE email=? AND (email=? OR manager_email=?) AND active=1",
                           (email,user["email"],user["email"])).fetchone())


def queue_manager_update(db,user,task,event):
    manager=user["manager_email"]
    if not manager:
        row=db.execute("SELECT manager_email FROM mailboxes WHERE mailbox=?",(task["mailbox"],)).fetchone()
        manager=row[0] if row else None
    if manager:
        db.execute("INSERT OR IGNORE INTO outbox(dedupe_key,recipient,kind,payload) VALUES(?,?,?,?)",
          (f"task:{task['id']}:{event}:{time.time_ns()}",manager,"task_update",json.dumps({"task_id":task["id"],"event":event})))


def handle_message(bot, db, update, extractor):
    msg=update.get("message",{}); chat=msg.get("chat",{}); tg_id=str(msg.get("from",{}).get("id","")); chat_id=chat.get("id")
    text=(msg.get("text") or "").strip()
    if not text or not chat_id: return
    if text.startswith("/link "):
        code=text.split(maxsplit=1)[1].strip()
        digest=hashlib.sha256(code.encode()).hexdigest()
        row=db.execute("SELECT * FROM binding_codes WHERE code_hash=? AND used_at IS NULL AND expires_at>?",(digest,datetime.now(timezone.utc).isoformat())).fetchone()
        if not row: bot.send(chat_id,"Код недействителен или истёк. Попросите администратора выдать новый."); return
        db.execute("UPDATE employees SET telegram_id=?,binding_confirmed=1 WHERE email=?",(tg_id,row["email"]))
        db.execute("UPDATE binding_codes SET used_at=? WHERE code_hash=?",(datetime.now(timezone.utc).isoformat(),digest))
        audit(db,row["email"],"telegram_bound",metadata={"telegram_id":tg_id}); db.commit()
        bot.send(chat_id,"Привязка подтверждена. Используйте /help для списка команд."); return
    user=linked_user(db,tg_id)
    if not user: bot.send(chat_id,"Аккаунт не привязан. Введите одноразовый код: /link КОД"); return
    if text in ("/start","/help","/help@"+"bot"):
        bot.send(chat_id,"Команды: /today · /tasks · /status ID СТАТУС · /clarify ID текст · /block ID причина · /help_task ID вопрос\nРуководителю: /review · /assign ID email · /report YYYY-MM-DD YYYY-MM-DD\nСтатусы: "+", ".join(STATUSES)); return
    if text in ("/today","/tasks"):
        rows=tasks_for(db,tg_id)
        if text=="/today":
            tz=ZoneInfo(os.getenv("ORG_TIMEZONE") or "UTC")
            today=datetime.now(tz).date().isoformat()
            rows=[r for r in rows if r["status"] not in ("Выполнена","Отменена") and (not r["due_at"] or r["due_at"][:10]<=today)]
        bot.send(chat_id,"\n".join(f"#{r['id']} [{r['status']}] {r['title']} — {r['due_at'] or 'без срока'}" for r in rows)[:3800] or "Задач нет."); return
    if text=="/review":
        if user["role"] not in ("manager","admin"): bot.send(chat_id,"Недостаточно прав."); return
        if user["role"]=="admin":
            rows=db.execute("SELECT * FROM tasks WHERE needs_review=1 ORDER BY received_at DESC LIMIT 30").fetchall()
        else:
            rows=db.execute("""SELECT t.* FROM tasks t LEFT JOIN mailboxes b ON b.mailbox=t.mailbox
              WHERE t.needs_review=1 AND (t.assignee_email=? OR t.assignee_email IN
              (SELECT email FROM employees WHERE manager_email=?) OR b.manager_email=?)
              ORDER BY t.received_at DESC LIMIT 30""",(user["email"],user["email"],user["email"])).fetchall()
        bot.send(chat_id,"Очередь проверки:\n"+("\n".join(f"#{r['id']} {r['title']} (предлагаемый исполнитель: {r['proposed_assignee_email'] or r['assignee_email'] or 'не определён'}, источник {r['sender']})" for r in rows) or "Очередь пуста.")+"\nПодтвердить: /approve ID · переназначить: /assign ID employee@org")
        return
    if text.startswith("/approve "):
        if user["role"] not in ("manager","admin"): bot.send(chat_id,"Недостаточно прав."); return
        m=re.fullmatch(r"/approve\s+(\d+)",text)
        if not m: bot.send(chat_id,"Формат: /approve ID"); return
        task=db.execute("SELECT * FROM tasks WHERE id=? AND needs_review=1",(int(m[1]),)).fetchone()
        if not task or not manager_owns_task(db,user,task): bot.send(chat_id,"Задача недоступна для проверки."); return
        assignee=task["proposed_assignee_email"] or task["assignee_email"]
        employee=db.execute("SELECT email FROM employees WHERE email=? AND active=1",(assignee,)).fetchone() if assignee else None
        if not employee: bot.send(chat_id,"Исполнитель не определён. Назначьте его: /assign ID employee@org"); return
        if not manager_owns_employee(db,user,assignee): bot.send(chat_id,"Предлагаемый исполнитель не входит в вашу команду."); return
        db.execute("UPDATE tasks SET assignee_email=?,proposed_assignee_email=NULL,needs_review=0,status='Новая',updated_at=CURRENT_TIMESTAMP WHERE id=?",(assignee,task["id"]))
        audit(db,user["email"],"task_approved",task["id"],{"assignee":assignee})
        db.execute("INSERT OR IGNORE INTO outbox(dedupe_key,recipient,kind,payload) VALUES(?,?,?,?)",(f"task:{task['id']}:new",assignee,"new_task",f'{{"task_id":{task["id"]}}}')); db.commit()
        bot.send(chat_id,"Задача подтверждена; уведомление сотруднику поставлено в очередь."); return
    if text.startswith("/assign "):
        if user["role"] not in ("manager","admin"): bot.send(chat_id,"Недостаточно прав."); return
        m=re.fullmatch(r"/assign\s+(\d+)\s+(\S+)",text)
        if not m: bot.send(chat_id,"Формат: /assign ID рабочий-email"); return
        tid,email=int(m[1]),m[2].lower()
        employee=db.execute("SELECT * FROM employees WHERE email=? AND active=1",(email,)).fetchone()
        task=db.execute("SELECT * FROM tasks WHERE id=? AND needs_review=1",(tid,)).fetchone()
        if not employee or not task: bot.send(chat_id,"Не найдена активная учётная запись или задача на проверке."); return
        if not manager_owns_employee(db,user,email): bot.send(chat_id,"Назначать можно только участнику вашей команды."); return
        if not manager_owns_task(db,user,task):
            # A mailbox manager may assign an unassigned item to any employee in their roster.
            mailbox=db.execute("SELECT manager_email FROM mailboxes WHERE mailbox=?",(task["mailbox"],)).fetchone()
            if not (user["role"]=="manager" and mailbox and mailbox[0]==user["email"] and not task["proposed_assignee_email"]):
                bot.send(chat_id,"У задачи нет связи с вашей командой; назначение отклонено."); return
        db.execute("UPDATE tasks SET assignee_email=?,proposed_assignee_email=NULL,needs_review=0,status='Новая',updated_at=CURRENT_TIMESTAMP WHERE id=?",(email,tid))
        audit(db,user["email"],"task_assigned",tid,{"assignee":email});
        db.execute("INSERT OR IGNORE INTO outbox(dedupe_key,recipient,kind,payload) VALUES(?,?,?,?)",(f"task:{tid}:new",email,"new_task",f'{{"task_id":{tid}}}')); db.commit()
        bot.send(chat_id,"Назначение записано. Уведомление уйдёт после подтверждённой Telegram-привязки."); return
    if text.startswith("/report "):
        m=re.fullmatch(r"/report\s+(\d{4}-\d\d-\d\d)\s+(\d{4}-\d\d-\d\d)",text)
        if not m: bot.send(chat_id,"Формат: /report YYYY-MM-DD YYYY-MM-DD (конец периода исключается)"); return
        try: rows=report(db,tg_id,m[1],m[2]+"T23:59:59")
        except PermissionError: bot.send(chat_id,"Недостаточно прав."); return
        audit(db,user["email"],"report_requested",metadata={"start":m[1],"end":m[2]}); db.commit()
        bot.send(chat_id,"Отчёт по сохранённым задачам; полнота зависит от подключённых ящиков:\n"+("\n".join(f"#{r['id']} {r['assignee_email'] or 'без исполнителя'} [{r['status']}] {r['title']} — {r['source_url'] or 'источник недоступен'}" for r in rows) or "Нет задач за период.")); return
    m=re.fullmatch(r"/status\s+(\d+)\s+(.+)",text)
    if m:
        status=m[2].strip()
        try: change_status(db,tg_id,int(m[1]),status); bot.send(chat_id,"Статус обновлён.")
        except (PermissionError,ValueError) as e: bot.send(chat_id,str(e))
        return
    m=re.fullmatch(r"/clarify\s+(\d+)\s+(.+)",text)
    if m:
        task=db.execute("SELECT * FROM tasks WHERE id=? AND assignee_email=?",(int(m[1]),user["email"])).fetchone()
        if not task: bot.send(chat_id,"Задача недоступна."); return
        db.execute("UPDATE tasks SET status='Ждёт ответа/материалов' WHERE id=?",(task["id"],)); audit(db,user["email"],"clarification_requested",task["id"]); queue_manager_update(db,user,task,"clarification_requested"); db.commit()
        bot.send(chat_id,"Запрос уточнения сохранён в журнале."); return
    m=re.fullmatch(r"/block\s+(\d+)\s+(.+)",text)
    if m:
        task=db.execute("SELECT * FROM tasks WHERE id=? AND assignee_email=?",(int(m[1]),user["email"])).fetchone()
        if not task: bot.send(chat_id,"Задача недоступна."); return
        db.execute("UPDATE tasks SET status='Ждёт ответа/материалов' WHERE id=?",(task["id"],)); audit(db,user["email"],"blocker_reported",task["id"]); queue_manager_update(db,user,task,"blocker_reported"); db.commit(); bot.send(chat_id,"Препятствие сохранено."); return
    m=re.fullmatch(r"/help_task\s+(\d+)\s+(.+)",text)
    if m:
        task=db.execute("SELECT * FROM tasks WHERE id=? AND assignee_email=?",(int(m[1]),user["email"])).fetchone()
        if not task: bot.send(chat_id,"Задача недоступна."); return
        try: answer=extractor.answer(m[2],task)
        except Exception: answer="Помощник временно не смог ответить. Повторите запрос позже."
        bot.send(chat_id,answer); return
    bot.send(chat_id,"Не понял команду. /help")


def handle_callback(bot,db,update):
    q=update.get("callback_query",{}); data=q.get("data",""); tg_id=str(q.get("from",{}).get("id","")); chat=q.get("message",{}).get("chat",{}).get("id")
    user=linked_user(db,tg_id)
    review=re.fullmatch(r"approve:(\d+)",data)
    if review:
        task=db.execute("SELECT * FROM tasks WHERE id=? AND needs_review=1",(int(review[1]),)).fetchone()
        if not user or user["role"] not in ("manager","admin") or not task or not manager_owns_task(db,user,task):
            bot.answer_callback(q.get("id",""),"Нет доступа к проверке"); return
        assignee=task["proposed_assignee_email"] or task["assignee_email"]
        if not assignee:
            bot.answer_callback(q.get("id",""),"Исполнитель не определён; используйте /assign"); return
        if not manager_owns_employee(db,user,assignee): bot.answer_callback(q.get("id",""),"Исполнитель вне вашей команды"); return
        db.execute("UPDATE tasks SET assignee_email=?,proposed_assignee_email=NULL,needs_review=0,status='Новая',updated_at=CURRENT_TIMESTAMP WHERE id=?",(assignee,task["id"]))
        audit(db,user["email"],"task_approved",task["id"],{"assignee":assignee})
        db.execute("INSERT OR IGNORE INTO outbox(dedupe_key,recipient,kind,payload) VALUES(?,?,?,?)",(f"task:{task['id']}:new",assignee,"new_task",f'{{"task_id":{task["id"]}}}')); db.commit()
        bot.answer_callback(q.get("id",""),"Задача подтверждена"); return
    m=re.fullmatch(r"(accept|clarify|wrong):(\d+)",data)
    if not user or not m: bot.answer_callback(q.get("id",""),"Требуется привязка"); return
    action,tid=m[1],int(m[2]); task=db.execute("SELECT * FROM tasks WHERE id=? AND assignee_email=?",(tid,user["email"])).fetchone()
    if not task: bot.answer_callback(q.get("id",""),"Задача недоступна"); return
    if action=="accept":
        change_status(db,tg_id,tid,"Принята"); bot.answer_callback(q["id"],"Принято")
    elif action=="clarify":
        change_status(db,tg_id,tid,"Ждёт ответа/материалов"); bot.answer_callback(q["id"],"Отправьте уточнение командой /clarify ID текст")
    else:
        db.execute("UPDATE tasks SET status='На проверке',needs_review=1,assignee_email=NULL WHERE id=?",(tid,)); audit(db,user["email"],"assignment_disputed",tid); queue_manager_update(db,user,task,"assignment_disputed"); db.commit(); bot.answer_callback(q["id"],"Передано на проверку")


def deliver_outbox(bot,db):
    for item in db.execute("SELECT * FROM outbox WHERE state='queued' ORDER BY id LIMIT 20").fetchall():
        user=db.execute("SELECT telegram_id,binding_confirmed FROM employees WHERE email=?",(item["recipient"],)).fetchone()
        if not user or not user["binding_confirmed"] or not user["telegram_id"]: continue
        task=db.execute("SELECT * FROM tasks WHERE id=?",(json.loads(item["payload"])["task_id"],)).fetchone()
        if not task: continue
        try:
            if item["kind"]=="review_task":
                proposed=task["proposed_assignee_email"] or task["assignee_email"]
                attachment_list=json.loads(task["attachments"] or "[]")
                attachment_text="\nВложения: "+", ".join(a["name"] for a in attachment_list[:5]) if attachment_list else ""
                text=f"Нужна проверка задачи #{task['id']}: {task['title']}\nОт: {task['sender']}\nПредлагаемый исполнитель: {proposed or 'не определён'}\nИсточник: {task['source_url'] or 'ссылка недоступна'}"+attachment_text
                keyboard=[[{"text":"Подтвердить","callback_data":f"approve:{task['id']}"}]] if proposed else None
                bot.send(user["telegram_id"],text,keyboard)
            elif item["kind"]=="task_update":
                event=json.loads(item["payload"]).get("event","обновление")
                bot.send(user["telegram_id"],f"Обновление задачи #{task['id']}: {event}. Текущий статус: {task['status']}.")
            else:
                send_task(bot,user["telegram_id"],task)
            db.execute("UPDATE outbox SET state='sent',attempts=attempts+1 WHERE id=?",(item["id"],)); db.commit()
        except Exception: db.execute("UPDATE outbox SET attempts=attempts+1 WHERE id=?",(item["id"],)); db.commit(); break


def run_bot():
    bot=Telegram(); db=connect(); init(db); extractor=Extractor()
    offset=int(db.execute("SELECT value FROM bot_state WHERE key='telegram_offset'").fetchone()[0]) if db.execute("SELECT value FROM bot_state WHERE key='telegram_offset'").fetchone() else 0
    while True:
        try:
            deliver_outbox(bot,db)
            for update in bot.updates(offset):
                offset=max(offset,int(update["update_id"])+1)
                try:
                    if "message" in update: handle_message(bot,db,update,extractor)
                    elif "callback_query" in update: handle_callback(bot,db,update)
                except Exception as exc:
                    print(f"telegram update failed: {type(exc).__name__}",file=sys.stderr)
                db.execute("INSERT OR REPLACE INTO bot_state(key,value) VALUES('telegram_offset',?)",(str(offset),)); db.commit()
        except KeyboardInterrupt: return
        except Exception: time.sleep(4)
