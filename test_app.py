import copy
import hashlib
import hmac
import io
import json
import os
import time
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch
import app


def resource(amount="203.00"):
    return {"id": "42", "attributes": {"transactions": [{
        "transaction_journal_id": "90", "type": "withdrawal",
        "source_id": "123", "destination_id": "999", "amount": amount,
        "date": "2026-10-14T12:00:00+00:00", "currency_code": "EUR",
        "description": "Purchase", "tags": ["imported"], "reconciled": False,
        "notes": "Keep me", "category_id": "10", "external_id": "bank-reference"
    }]}}


class Tests(unittest.TestCase):
    def test_amounts_and_metadata(self):
        r = resource()
        before = copy.deepcopy(r)
        p, _ = app.plan(r, "123", "456")
        a, b = p["transactions"]
        self.assertEqual((a["amount"], b["amount"]), ("200.00", "3.00"))
        self.assertEqual(a["transaction_journal_id"], "90")
        self.assertNotIn("transaction_journal_id", b)
        self.assertNotIn("external_id", b)
        self.assertEqual(b["destination_id"], "456")
        self.assertEqual(r, before)

    def test_rounding_conserves_total(self):
        for cents in range(1, 10001):
            total = Decimal(cents) / 100
            p, _ = app.plan(resource(str(total)), "123", "456")
            if p:
                a, b = p["transactions"]
                self.assertEqual(Decimal(a["amount"]) + Decimal(b["amount"]), total)
                self.assertEqual((Decimal(a["amount"]) * Decimal("0.015")).quantize(
                    Decimal("0.01"), rounding="ROUND_HALF_UP"), Decimal(b["amount"]))

    def test_skip_conditions(self):
        for changes in ({"type": "deposit"}, {"source_id": "124"},
                        {"currency_code": "USD"}, {"foreign_currency_id": "2"},
                        {"reconciled": True}, {"tags": [app.MARKER]},
                        {"amount": "0"}, {"amount": "-203"}):
            r = resource()
            r["attributes"]["transactions"][0].update(changes)
            self.assertIsNone(app.plan(r, "123", "456")[0])
        r = resource()
        r["attributes"]["transactions"] *= 2
        self.assertIsNone(app.plan(r, "123", "456")[0])

    def test_signature(self):
        raw = b'{"content":{"id":"42"}}'
        sig = hmac.new(b"secret", b"1000." + raw, hashlib.sha3_256).hexdigest()
        header = "t=1000,v1=" + sig
        self.assertTrue(app.valid_signature(raw, header, "secret", now=1000))
        self.assertFalse(app.valid_signature(raw + b" ", header, "secret", now=1000))
        self.assertFalse(app.valid_signature(raw, header, "secret", now=1400))
        self.assertFalse(app.valid_signature(raw, "broken", "secret", now=1000))

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "FINOM_ACCOUNT_ID": "123", "FEE_EXPENSE_ACCOUNT_ID": "456",
            "WEBHOOK_SECRET": "secret", "DRY_RUN": "false",
            "QUEUE_DB_PATH": self.temp.name + "/queue.sqlite3",
            "AI_WAIT_SECONDS": "60", "QUEUE_POLL_SECONDS": "5",
            "AI_TAG_PREFIX": "ai:",
        })
        self.env.start()
        self.clock = patch.object(app.time, "time", return_value=1000)
        self.clock.start()
        self.state = resource()
        self.writes = []
        self.calls = []
        def fake_api(method, tid, payload=None):
            self.calls.append(method)
            if method == "PUT":
                self.writes.append(payload)
                self.state["attributes"]["transactions"][0].update(payload["transactions"][0])
                self.state["attributes"]["transactions"].append(payload["transactions"][1])
            return {"data": copy.deepcopy(self.state)}
        self.api = patch.object(app, "api", side_effect=fake_api)
        self.api.start()
        app.init_database()

    def tearDown(self):
        self.api.stop()
        self.clock.stop()
        self.env.stop()
        self.temp.cleanup()

    def job(self):
        with app.database() as db:
            row = db.execute("SELECT * FROM jobs WHERE transaction_id='42'").fetchone()
        return dict(row) if row else None

    def test_dry_run_and_invalid_not_queued(self):
        os.environ["DRY_RUN"] = "true"
            self.assertEqual(app.process("42"), "dry-run")
        self.assertIsNone(self.job())
            os.environ["DRY_RUN"] = "false"
        self.state["attributes"]["transactions"][0]["type"] = "deposit"
        self.assertTrue(app.process("42").startswith("skipped"))
        self.assertIsNone(self.job())
        self.assertEqual(self.writes, [])

    def test_wait_then_use_fresh_tags_category_and_title(self):
        self.assertEqual(app.process("42"), "queued")
        app.worker_tick()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.job()["state"], "pending")
        row = self.state["attributes"]["transactions"][0]
        row.update(tags=["todo", "AI groceries", "new-tag"], category_id="77")
        self.state["attributes"]["group_title"] = "Fresh title"
        with patch.object(app.time, "time", return_value=1006):
            app.worker_tick()
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.job()["state"], "completed")
        payload = self.writes[0]
        self.assertEqual(payload["group_title"], "Fresh title")
        for split in payload["transactions"]:
            self.assertIn("new-tag", split["tags"])
            self.assertIn("AI groceries", split["tags"])
        self.assertNotIn("category_id", payload["transactions"][0])
        self.assertEqual(row["category_id"], "77")
        self.assertFalse(payload["fire_webhooks"])
        with patch.object(app.time, "time", return_value=1012):
            app.worker_tick()
        self.assertEqual(len(self.writes), 1)

    def test_persistence_duplicates_and_expiry(self):
        app.process("42")
        deadline = self.job()["deadline"]
        app.init_database()  # Reopen/initialize as after process restart.
        with patch.object(app.time, "time", return_value=1030):
            app.process("42")
        self.assertEqual(self.job()["deadline"], deadline)
        self.state["attributes"]["transactions"][0]["tags"] = ["ai-ready"]
        with patch.object(app.time, "time", return_value=1060):
            app.worker_tick()
            self.assertEqual(app.process("42"), "expired")
        self.assertEqual(self.job()["state"], "expired")
        self.assertEqual(self.writes, [])

    def test_validation_repeated_before_put(self):
        app.process("42")
        row = self.state["attributes"]["transactions"][0]
        row.update(reconciled=True, tags=["ai-done"])
        app.worker_tick()
        self.assertEqual(self.job()["state"], "discarded")
        self.assertEqual(self.writes, [])

    def test_transient_error_keeps_job_then_recovers(self):
        app.process("42")
        with patch.object(app, "api", side_effect=OSError("offline")):
            app.worker_tick()
        self.assertEqual(self.job()["state"], "pending")
        self.state["attributes"]["transactions"][0]["tags"] = ["ai"]
        with patch.object(app.time, "time", return_value=1006):
            app.worker_tick()
        self.assertEqual(self.job()["state"], "completed")

    def test_timeout_during_fetch_does_not_put(self):
        app.process("42")
        self.state["attributes"]["transactions"][0]["tags"] = ["ai-ready"]
        with patch.object(app.time, "time", side_effect=[1059, 1061]):
            app.handle_job(self.job())
        self.assertEqual(self.job()["state"], "expired")
        self.assertEqual(self.writes, [])

    def test_recovery_after_successful_put_before_ack(self):
        app.process("42")
        self.state["attributes"]["transactions"] *= 2
        app.worker_tick()
        self.assertEqual(self.job()["state"], "discarded")
        self.assertEqual(self.writes, [])

    def test_webhook_enqueues_without_put(self):
            raw = json.dumps({"content": {"id": "42"}}).encode()
        sig = hmac.new(b"secret", b"1000." + raw, hashlib.sha3_256).hexdigest()
                statuses = []
                response = app.application({"PATH_INFO": "/webhook", "REQUEST_METHOD": "POST",
                    "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw),
            "HTTP_SIGNATURE": "t=1000,v1=" + sig},
                    lambda status, headers: statuses.append(status))
                self.assertEqual(statuses, ["200 OK"])
        self.assertEqual(json.loads(b"".join(response))["result"]["42"], "queued")
        self.assertEqual(self.calls, ["GET"])
        self.assertEqual(self.job()["state"], "pending")

    def test_unauthorized_webhook_no_api_or_queue(self):
        raw = b'{"content":{"id":"42"}}'
        statuses = []
        app.application({"PATH_INFO": "/webhook", "REQUEST_METHOD": "POST",
            "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw),
            "HTTP_SIGNATURE": "invalid"}, lambda status, headers: statuses.append(status))
        self.assertEqual(statuses, ["401 Unauthorized"])
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.job())


if __name__ == "__main__":
    unittest.main()