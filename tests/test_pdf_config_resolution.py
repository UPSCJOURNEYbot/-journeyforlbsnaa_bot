"""
Deployment-critical config resolution for the PDF microservice.

The standard VPS topology runs the bot and the PDF renderer
(`quizbot-pdf.service`) on the same host, with the renderer on
127.0.0.1:8090. To make that topology work out of the box -- and to make the
dead-end message "PDF generation is not configured ... (no PDF_API_BASE set)"
unreachable in the standard deployment -- a blank/unset PDF_API_BASE must
resolve to the local service, while an explicit opt-out sentinel disables it.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from quizbot.shared import config


class PdfApiBaseResolutionTests(unittest.TestCase):
    def _resolve(self, value):
        env = dict(os.environ)
        if value is None:
            env.pop("PDF_API_BASE", None)
        else:
            env["PDF_API_BASE"] = value
        with patch.dict(os.environ, env, clear=True):
            return config._resolve_pdf_api_base()

    def test_unset_defaults_to_local_service(self):
        self.assertEqual(
            self._resolve(None), "http://127.0.0.1:8090")

    def test_blank_defaults_to_local_service(self):
        for blank in ("", "   ", "\t"):
            self.assertEqual(
                self._resolve(blank), "http://127.0.0.1:8090", blank)

    def test_explicit_disable_sentinels_return_none(self):
        for tok in ("off", "OFF", " Off ", "none", "disabled", "false", "0"):
            self.assertIsNone(self._resolve(tok), tok)

    def test_remote_url_preserved_without_trailing_slash(self):
        self.assertEqual(
            self._resolve("https://pdf.example.com/"),
            "https://pdf.example.com")
        self.assertEqual(
            self._resolve("http://10.0.0.5:8090/"),
            "http://10.0.0.5:8090")

    def test_module_default_points_at_local_service(self):
        # The imported config must already be the standard deployment default
        # unless the test environment explicitly overrode it.
        if not os.getenv("PDF_API_BASE"):
            self.assertEqual(config.PDF_API_BASE, "http://127.0.0.1:8090")
        elif os.getenv("PDF_API_BASE", "").lower() in {
                "off", "none", "disabled", "false", "0"}:
            self.assertIsNone(config.PDF_API_BASE)


if __name__ == "__main__":
    unittest.main()
