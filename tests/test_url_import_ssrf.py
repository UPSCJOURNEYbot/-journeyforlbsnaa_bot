"""
SSRF / input-safety tests for the creator URL importer.

``process_public_url`` fetches a creator-supplied link server-side; without a
host allowlist it could be pointed at the loopback PDF service, RFC19118
internal hosts, or the 169.254.169.254 cloud-metadata endpoint (with the
response body reflected back to the caller as imported questions). These
tests pin the fail-closed guard (pure, no network -- IP literals avoid DNS).
"""

from __future__ import annotations

import unittest

from quizbot.creator_bot.handlers.file_import import (
    _PUBLIC_URL_MAX_BYTES,
    _assert_public_http_url,
)


class SsrfGuardTests(unittest.TestCase):
    def test_public_ip_literal_allowed(self):
        # 1.1.1.1 / 8.8.8.8 are global addresses; getaddrinfo on an IP
        # literal needs no DNS.
        for url in ("http://1.1.1.1/path", "https://8.8.8.8/a?b=1"):
            _assert_public_http_url(url)  # must not raise

    def test_loopback_blocked(self):
        for url in ("http://127.0.0.1/", "http://127.0.0.1:8090/healthz",
                    "http://[::1]/", "http://localhost/x"):
            with self.assertRaises(ValueError, msg=url):
                _assert_public_http_url(url)

    def test_private_ranges_blocked(self):
        for url in ("http://10.0.0.5/", "http://192.168.1.10/",
                    "http://172.16.4.5/", "http://[fc00::1]/"):
            with self.assertRaises(ValueError, msg=url):
                _assert_public_http_url(url)

    def test_cloud_metadata_link_local_blocked(self):
        with self.assertRaises(ValueError):
            _assert_public_http_url("http://169.254.169.254/latest/meta-data/")
        with self.assertRaises(ValueError):
            _assert_public_http_url(
                "http://metadata.google.internal/computeMetadata/v1/")

    def test_unspecified_and_internal_suffixes_blocked(self):
        for url in ("http://0.0.0.0/", "http://host.local/",
                    "http://service.internal/"):
            with self.assertRaises(ValueError, msg=url):
                _assert_public_http_url(url)

    def test_non_http_schemes_blocked(self):
        for url in ("file:///etc/passwd", "ftp://1.1.1.1/x",
                    "gopher://1.1.1.1", "javascript:alert(1)", "not a url"):
            with self.assertRaises(ValueError, msg=url):
                _assert_public_http_url(url)

    def test_response_size_cap_is_sane(self):
        # Bounded so an oversized/chunked response cannot exhaust memory.
        self.assertLessEqual(_PUBLIC_URL_MAX_BYTES, 25 * 1024 * 1024)
        self.assertGreater(_PUBLIC_URL_MAX_BYTES, 1024)


if __name__ == "__main__":
    unittest.main()
