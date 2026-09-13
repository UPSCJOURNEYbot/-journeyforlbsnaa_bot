"""
Mini App access-denied batch payload safety.

The player Mini App shows a creator-configured batch card (name, description,
contact, "Get access" payment link) when access is denied. The creator is
semi-trusted relative to the player, so a stored batch must not be able to push
a ``javascript:``/``data:`` href or unbounded text into the page. The server
projects/sanitises the batch (``_safe_batch_payload``); these tests pin that.
"""

from __future__ import annotations

import unittest

from quizbot.mini_app import routes


class SafeBatchPayloadTests(unittest.TestCase):
    def test_well_formed_batch_passes_through(self):
        batch = {
            "name": "UPSC 2026",
            "description": "Full batch",
            "payment_link": "https://pay.example.com/checkout/123",
            "contact_info": "@admin",
            "extra_ignored_field": "should-not-leak",
        }
        out = routes._safe_batch_payload(batch)
        self.assertEqual(out["name"], "UPSC 2026")
        self.assertEqual(out["description"], "Full batch")
        self.assertEqual(out["contact_info"], "@admin")
        self.assertEqual(
            out["payment_link"], "https://pay.example.com/checkout/123")
        self.assertNotIn("extra_ignored_field", out)

    def test_dangerous_link_schemes_are_stripped(self):
        for bad in ("javascript:alert(document.cookie)",
                    "  JavaScript:alert(1)",
                    "data:text/html,<script>x</script>",
                    "vbscript:msgbox(1)",
                    "//evil.example.com/pay",
                    "/relative/path",
                    ""):
            out = routes._safe_batch_payload({"name": "n", "payment_link": bad})
            self.assertIsNone(out["payment_link"], repr(bad))

    def test_telegram_and_http_schemes_allowed(self):
        for good in ("https://pay.example.com/x",
                     "http://pay.example.com/x",
                     "tg://resolve?domain=support"):
            out = routes._safe_batch_payload({"payment_link": good})
            self.assertEqual(out["payment_link"], good, good)

    def test_text_fields_are_bounded_and_typed(self):
        out = routes._safe_batch_payload({
            "name": "x" * 5000,
            "description": 12345,            # non-string -> None
            "contact_info": "  @help  ",
        })
        self.assertEqual(len(out["name"]), routes._BATCH_FIELD_CAP)
        self.assertIsNone(out["description"])
        self.assertEqual(out["contact_info"], "@help")

    def test_non_batch_yields_none(self):
        self.assertIsNone(routes._safe_batch_payload(None))
        self.assertIsNone(routes._safe_batch_payload("not-a-dict"))
        self.assertIsNone(routes._safe_batch_payload(123))


if __name__ == "__main__":
    unittest.main()
