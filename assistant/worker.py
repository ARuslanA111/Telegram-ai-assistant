from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .core import ingest
from .db import connect, init
from .integrations import Extractor, Graph


def sync_once():
    if os.getenv("APP_MODE", "demo").lower() != "live":
        print("Demo mode: Graph sync skipped."); return
    db=connect(); init(db); graph=Graph(); extractor=Extractor()
    roster=[r[0] for r in db.execute("SELECT email FROM employees WHERE active=1")]
    for box in db.execute("SELECT * FROM mailboxes").fetchall():
        try:
            msgs,cursor=graph.mailbox_delta(box["mailbox"],box["cursor"],box["connected_at"])
            for msg in msgs:
                from_addr=(msg.get("from") or {}).get("emailAddress",{}).get("address","")
                # Include only messages received by a configured employee or flagged important;
                # forwarded threads are considered by the extractor, never trusted for access.
                recipients=[x.get("emailAddress",{}).get("address","").lower() for x in msg.get("toRecipients",[])]
                if not set(r.lower() for r in roster).intersection(recipients) and msg.get("importance")!="high": continue
                attachments=graph.message_attachments(box["mailbox"],msg["id"]) if msg.get("hasAttachments") else []
                allowed={x.lower().strip() for x in os.getenv("ALLOWED_FILE_EXTENSIONS",".pdf,.docx,.xlsx,.pptx,.txt").split(",")}
                max_bytes=int(os.getenv("MAX_FILE_MB","15"))*1024*1024
                for item in attachments:
                    suffix=os.path.splitext(item["name"])[1].lower()
                    item["allowed_by_policy"]=bool(suffix in allowed and item["size"]<=max_bytes)
                extraction=extractor.extract({**msg,"attachments":attachments},roster)
                message={"id":msg["id"],"conversation_id":msg.get("conversationId"),"sender":from_addr,
                         "received_at":msg.get("receivedDateTime"),"source_url":msg.get("webLink"),"attachments":attachments}
                ingest(db,box["mailbox"],message,extraction)
            db.execute("UPDATE mailboxes SET cursor=?,last_sync=?,last_error=NULL WHERE mailbox=?",
                       (cursor,datetime.now(timezone.utc).isoformat(),box["mailbox"]))
        except Exception as exc:
            # Keep diagnostics free from request URLs, tokens, and message body.
            db.execute("UPDATE mailboxes SET last_sync=?,last_error=? WHERE mailbox=?",
                       (datetime.now(timezone.utc).isoformat(),type(exc).__name__,box["mailbox"]))
        db.commit()


def schedule_loop():
    tz_name=os.getenv("ORG_TIMEZONE","").strip()
    if not tz_name:
        print("Scheduler disabled: set ORG_TIMEZONE to the organization's approved time zone.")
        return
    tz=ZoneInfo(tz_name)
    times={x.strip() for x in os.getenv("POLL_TIMES","09:00,13:00,17:00").split(",") if x.strip()}
    if len(times)!=3 or any(len(t)!=5 or t[2] != ":" for t in times):
        raise ValueError("POLL_TIMES must contain exactly three HH:MM values")
    seen=None
    while True:
        now=datetime.now(tz); key=(now.date().isoformat(),now.strftime("%H:%M"))
        if now.strftime("%H:%M") in times and key!=seen:
            sync_once(); seen=key
        time.sleep(20)
