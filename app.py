"""Finom fee splitter. Python 3.11+, standard library only."""
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from decimal import Decimal, ROUND_HALF_UP
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
    with build_opener(NoRedirect).open(req, timeout=15) as response:
        return json.load(response)


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
    total = Decimal(row["amount"])
    if not total.is_finite() or total <= 0 or total != total.quantize(Decimal("0.01")):
        return None, "unsupported amount"
    net = (total / Decimal("1.015")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    fee = total - net
    if net <= 0 or fee <= 0:
        return None, "fee rounds to zero"
    if (net * Decimal("0.015")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) != fee:
        return None, "total does not match rounded 1.5% fee"
    # Existing journal: only change amount, order and tags. Omitted metadata
    # stays on the existing journal rather than being copied onto the fee.
    original = {"transaction_journal_id": row["transaction_journal_id"],
                "amount": str(net), "order": 0,
                "tags": list(row.get("tags") or []) + [MARKER]}
    extra = {"type": "withdrawal", "date": row["date"],
             "amount": str(fee), "description": "Fee",
             "source_id": account_id, "destination_id": fee_account_id,
             "currency_code": "EUR",
             "tags": [MARKER], "order": 1,
             "notes": f"Fee included in imported total EUR {total}; net EUR {net}."}
    return {"apply_rules": False, "fire_webhooks": False,
            "transactions": [original, extra]}, "ready"


def process(transaction_id):
    current = api("GET", transaction_id)["data"]
    payload, reason = plan(current, os.environ["FINOM_ACCOUNT_ID"],
                           os.environ["FEE_EXPENSE_ACCOUNT_ID"])
    if payload is None:
        LOG.info("transaction=%s skipped=%s", transaction_id, reason)
        return "skipped: " + reason
    if os.environ.get("DRY_RUN", "true").lower() != "false":
        LOG.info("transaction=%s dry_run net=%s fee=%s", transaction_id,
                 payload["transactions"][0]["amount"], payload["transactions"][1]["amount"])
        return "dry-run"
    api("PUT", transaction_id, payload)
    LOG.info("transaction=%s split", transaction_id)
    return "split"


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
        # One worker per deployment. Serialize duplicate deliveries and refetch
        # current state under the lock before modifying anything.
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


if __name__ == "__main__":
    from wsgiref.simple_server import make_server
    logging.basicConfig(level=logging.INFO)
    for key in ("FIREFLY_URL", "FIREFLY_TOKEN", "FINOM_ACCOUNT_ID",
                "FEE_EXPENSE_ACCOUNT_ID", "WEBHOOK_SECRET", "PORT"):
        if not os.environ.get(key):
            raise SystemExit(f"Missing {key}")
    port = int(os.environ.get("PORT"))
    with make_server("0.0.0.0", port, application) as server:
        server.serve_forever()
