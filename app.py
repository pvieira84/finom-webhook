"""Finom fee splitter. Python 3.11+, standard library only."""
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import sqlite3
from contextlib import contextmanager
import threading
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPRedirectHandler

LOG = logging.getLogger("finom")
LOCK = threading.Lock()
MARKER = "finom-fee-split-v1"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def api(method, transaction_id, payload=None):
    base = os.environ["FIREFLY_URL"].rstrip("/")
    req = Request(
        f"{base}/api/v1/transactions/{transaction_id}",
        data=None if payload is None else json.dumps(payload).encode(),
        method=method,
        headers={"Authorization": "Bearer " + os.environ["FIREFLY_TOKEN"],
                 "Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with build_opener(NoRedirect).open(req, timeout=15) as response:
            return json.load(response)
    except HTTPError as exc:
        body = exc.read(4000).decode("utf-8", errors="replace")
        LOG.error("Firefly API method=%s transaction=%s status=%s server=%s content_type=%s",
                  method, transaction_id, exc.code, exc.headers.get("Server"),
                  exc.headers.get("Content-Type"))
        if os.environ.get("LOG_API_ERROR_BODY", "false").lower() == "true":
            LOG.error("Firefly response=%s", body)
        raise


def valid_signature(raw, signature, secret, now=None):
    try:
        parts = dict(part.strip().split("=", 1) for part in signature.split(","))
        stamp = parts["t"]
        if abs((time.time() if now is None else now) - int(stamp)) > 300:
            return False
        expected = hmac.new(secret.encode(), stamp.encode() + b"." + raw,
                            hashlib.sha3_256).hexdigest()
        return hmac.compare_digest(expected, parts["v1"])
    except (ValueError, KeyError):
        return False


def transaction_ids(event):
    # TRANSACTIONS response: content is a transaction resource. Also accept
    # resource arrays and JSON:API data wrappers used by integrations.
    content = event["content"]
    if isinstance(content, dict) and "data" in content:
        content = content["data"]
    resources = content if isinstance(content, list) else [content]
    ids = []
    for resource in resources:
        value = str(resource["id"])
        if not re.fullmatch(r"[1-9][0-9]*", value):
            raise ValueError("Invalid transaction group ID")
        ids.append(value)
    if not ids:
        raise ValueError("Empty transaction content")
    return list(dict.fromkeys(ids))


def plan(resource, account_id, fee_account_id):
    rows = resource["attributes"]["transactions"]
    if len(rows) != 1:
        return None, "already split"
    row = rows[0]
    if row["type"] != "withdrawal" or str(row["source_id"]) != account_id:
        return None, "not a Finom withdrawal"
    if MARKER in (row.get("tags") or []):
        return None, "already processed"
    if row.get("currency_code") != "EUR" or row.get("foreign_currency_id") or row.get("foreign_currency_code"):
        return None, "unsupported currency"
    if row.get("reconciled"):
        return None, "reconciled"
    try:
    total = Decimal(row["amount"])
    except (InvalidOperation, TypeError):
        return None, "unsupported amount"
    if not total.is_finite() or total <= 0 or total != total.quantize(Decimal("0.01")):
        return None, "unsupported amount"
    net = (total / Decimal("1.015")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    fee = total - net
    if net <= 0 or fee <= 0:
        return None, "fee rounds to zero"
    if (net * Decimal("0.015")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) != fee:
        return None, "total does not match rounded 1.5% fee"
    tags = list(dict.fromkeys([*(row.get("tags") or []), "todo", MARKER]))
    # Existing journal: only change amount, order and tags. Omitted metadata
    # stays on the existing journal rather than being copied onto the fee.
    original = {"transaction_journal_id": row["transaction_journal_id"],
                "amount": str(net), "order": 0,
                "tags": tags}
    extra = {"type": "withdrawal", "date": row["date"],
             "amount": str(fee), "description": "Fee",
             "source_id": account_id, "destination_id": fee_account_id,
             "currency_code": "EUR",
             "tags": tags, "order": 1,
             "notes": f"Fee included in imported total EUR {total}; net EUR {net}."}
    group_title = (resource["attributes"].get("group_title")
                   or row.get("description") or "Finom withdrawal")
    return {
        "group_title": group_title,
        "apply_rules": False,
        "fire_webhooks": False,
        "transactions": [original, extra],
    }, "ready"

def dry_run():
    return os.environ.get("DRY_RUN", "true").lower() != "false"


def positive_seconds(name, default):
    value = float(os.environ.get(name, default))
    if not 0 < value < float("inf"):
        raise ValueError(f"{name} must be a positive number")
    return value


@contextmanager
def database():
    connection = sqlite3.connect(os.environ.get("QUEUE_DB_PATH", "queue.sqlite3"), timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init_database():
    with database() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            transaction_id TEXT PRIMARY KEY,
            deadline REAL NOT NULL,
            next_check REAL NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            detail TEXT NOT NULL DEFAULT ''
        )""")


def finish(transaction_id, state, detail):
    with database() as db:
        db.execute("UPDATE jobs SET state=?, detail=? WHERE transaction_id=?",
                   (state, detail, transaction_id))
    LOG.info("transaction=%s state=%s detail=%s", transaction_id, state, detail)


def process(transaction_id):
    # Validate live state before durable admission. Never PUT in the HTTP handler.
    current = api("GET", transaction_id)["data"]
    payload, reason = plan(current, os.environ["FINOM_ACCOUNT_ID"],
                           os.environ["FEE_EXPENSE_ACCOUNT_ID"])
    if payload is None:
        LOG.info("transaction=%s skipped=%s", transaction_id, reason)
        return "skipped: " + reason
    if dry_run():
        LOG.info("transaction=%s dry_run net=%s fee=%s", transaction_id,
                 payload["transactions"][0]["amount"], payload["transactions"][1]["amount"])
        return "dry-run"
    now = time.time()
    deadline = now + positive_seconds("AI_WAIT_SECONDS", "300")
    with database() as db:
        # Duplicates cannot extend the original deadline or resurrect expired work.
        db.execute("INSERT OR IGNORE INTO jobs(transaction_id, deadline, next_check) VALUES(?, ?, ?)",
                   (transaction_id, deadline, now))
        job = db.execute("SELECT state FROM jobs WHERE transaction_id=?", (transaction_id,)).fetchone()
    LOG.info("transaction=%s queue_state=%s", transaction_id, job["state"])
    return "queued" if job["state"] == "pending" else job["state"]


def has_ai_tag(resource):
    tags = resource["attributes"]["transactions"][0].get("tags") or []
    prefix = os.environ.get("AI_TAG_PREFIX", "ai:").strip().casefold()
    return any(str(tag).strip().casefold().startswith(prefix) for tag in tags)


def handle_job(job):
    tid = job["transaction_id"]
    if time.time() >= job["deadline"]:
        finish(tid, "expired", "AI wait deadline reached; removed from pending queue")
        return
    if dry_run():
        finish(tid, "cancelled", "DRY_RUN enabled; no update")
        return
    try:
        current = api("GET", tid)["data"]
        payload, reason = plan(current, os.environ["FINOM_ACCOUNT_ID"],
                               os.environ["FEE_EXPENSE_ACCOUNT_ID"])
        if payload is None:
            finish(tid, "discarded", reason)
            return
        if time.time() >= job["deadline"]:
            finish(tid, "expired", "deadline reached during fetch")
            return
        if not has_ai_tag(current):
            LOG.info("transaction=%s waiting_for_ai seconds_left=%.0f", tid,
                     job["deadline"] - time.time())
            return
        # Payload is built from this fetch, not the original webhook snapshot.
        api("PUT", tid, payload)
        finish(tid, "completed", "split after AI tag")
    except HTTPError as exc:
        if exc.code == 404:
            finish(tid, "discarded", "transaction no longer exists")
        else:
            LOG.error("transaction=%s API HTTP %s; retry before deadline", tid, exc.code)
    except Exception as exc:
        LOG.error("transaction=%s worker failure=%s; retry before deadline", tid, type(exc).__name__)


def worker_tick():
    now = time.time()
    interval = positive_seconds("QUEUE_POLL_SECONDS", "5")
    with database() as db:
        # Expire all overdue work, including jobs behind a slow API request.
        expired = db.execute("SELECT transaction_id FROM jobs WHERE state='pending' AND deadline<=?",
                             (now,)).fetchall()
        db.execute("UPDATE jobs SET state='expired', detail='AI wait deadline reached' "
                   "WHERE state='pending' AND deadline<=?", (now,))
        due = db.execute("SELECT * FROM jobs WHERE state='pending' AND next_check<=? "
                         "ORDER BY next_check LIMIT 50", (now,)).fetchall()
    for row in expired:
        LOG.info("transaction=%s state=expired removed_from_pending_queue", row["transaction_id"])
    for job in due:
        handle_job(job)
        with database() as db:
            db.execute("UPDATE jobs SET next_check=? WHERE transaction_id=? AND state='pending'",
                       (time.time() + interval, job["transaction_id"]))


def worker_loop(stop):
    while not stop.is_set():
        try:
            worker_tick()
        except Exception as exc:
            LOG.error("queue worker failure=%s", type(exc).__name__)
        stop.wait(0.5)


def application(environ, start_response):
    def reply(code, message):
        body = json.dumps({"result": message}).encode()
        start_response(code, [("Content-Type", "application/json"),
                              ("Content-Length", str(len(body)))])
        return [body]
    if environ.get("PATH_INFO") == "/health" and environ["REQUEST_METHOD"] == "GET":
        return reply("200 OK", "ok")
    if environ.get("PATH_INFO") != "/webhook" or environ["REQUEST_METHOD"] != "POST":
        return reply("404 Not Found", "not found")
    try:
        length = int(environ.get("CONTENT_LENGTH") or "0")
        if not 0 < length <= 1048576:
            return reply("413 Payload Too Large", "invalid body size")
        raw = environ["wsgi.input"].read(length)
        if not valid_signature(raw, environ.get("HTTP_SIGNATURE", ""), os.environ["WEBHOOK_SECRET"]):
            return reply("401 Unauthorized", "invalid signature")
        event = json.loads(raw)
        ids = transaction_ids(event)
    except (ValueError, KeyError, TypeError):
        return reply("400 Bad Request", "expected TRANSACTIONS webhook payload")
    try:
        # Return after validation and durable enqueue, without waiting for AI.
        with LOCK:
            results = {tid: process(tid) for tid in ids}
        return reply("200 OK", results)
    except HTTPError as exc:
        LOG.error("Firefly API returned HTTP %s", exc.code)
        return reply("502 Bad Gateway", "Firefly API error; inspect Firefly logs")
    except Exception:
        # Do not log tokens, raw webhook bodies, or API error bodies.
        LOG.error("Processing failed; check connectivity and configuration")
        return reply("503 Service Unavailable", "processing failed")


def main():
    from wsgiref.simple_server import make_server
    logging.basicConfig(level=logging.INFO)
    for key in ("FIREFLY_URL", "FIREFLY_TOKEN", "FINOM_ACCOUNT_ID",
                "FEE_EXPENSE_ACCOUNT_ID", "WEBHOOK_SECRET", "PORT"):
        if not os.environ.get(key):
            raise SystemExit(f"Missing {key}")
    positive_seconds("AI_WAIT_SECONDS", "300")
    positive_seconds("QUEUE_POLL_SECONDS", "5")
    if not os.environ.get("AI_TAG_PREFIX", "ai:").strip():
        raise SystemExit("AI_TAG_PREFIX must not be empty")
    init_database()
    stop = threading.Event()
    worker = threading.Thread(target=worker_loop, args=(stop,), name="queue-worker", daemon=True)
    def terminate(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    with make_server("0.0.0.0", int(os.environ["PORT"]), application) as server:
        worker.start()
        try:
        server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            worker.join(timeout=20)


if __name__ == "__main__":
    main()