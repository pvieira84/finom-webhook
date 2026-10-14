import copy
import hashlib
import hmac
import io
import json
import os
import time
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

    def test_delivery_and_retry(self):
        state = resource()
        writes = []
        def fake_api(method, tid, payload=None):
            self.assertEqual(tid, "42")
            if method == "PUT":
                writes.append(payload)
                state["attributes"]["transactions"][0].update(payload["transactions"][0])
                state["attributes"]["transactions"].append(payload["transactions"][1])
            return {"data": copy.deepcopy(state)}
        env = {"FINOM_ACCOUNT_ID": "123", "FEE_EXPENSE_ACCOUNT_ID": "456",
               "WEBHOOK_SECRET": "secret", "DRY_RUN": "true"}
        with patch.dict(os.environ, env), patch.object(app, "api", side_effect=fake_api):
            self.assertEqual(app.process("42"), "dry-run")
            self.assertEqual(writes, [])
            os.environ["DRY_RUN"] = "false"
            raw = json.dumps({"content": {"id": "42"}}).encode()
            stamp = str(int(time.time()))
            sig = hmac.new(b"secret", stamp.encode() + b"." + raw, hashlib.sha3_256).hexdigest()
            for _ in range(2):
                statuses = []
                response = app.application({"PATH_INFO": "/webhook", "REQUEST_METHOD": "POST",
                    "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw),
                    "HTTP_SIGNATURE": f"t={stamp},v1={sig}"},
                    lambda status, headers: statuses.append(status))
                self.assertEqual(statuses, ["200 OK"])
                self.assertTrue(json.loads(b"".join(response)))
            self.assertEqual(len(writes), 1)


if __name__ == "__main__":
    unittest.main()
