"""IPv4-only Gemini transport — regression tests (offline).

The VPS has a blackholed IPv6 path: ``curl -4`` to
``generativelanguage.googleapis.com`` answers immediately, ``curl -6`` times
out, and an AF_UNSPEC ``getaddrinfo()`` for that host stalls. google-genai hands
httpx the hostname and httpcore resolves it with AF_UNSPEC, so a Gemini call can
burn the whole 150s HTTP timeout on IPv6 before IPv4 is tried — that is what
pushed /podcast past its 180s bound and surfaced as ``kind=transient``.

These tests pin the fix and its blast radius:

* ``_new_genai_client`` still passes ``HttpOptions(timeout=150000)`` AND an
  IPv4-only httpx transport for the Gemini client.
* Resolution for Gemini uses AF_INET only (no AAAA query) and httpcore dials
  IPv4 *literals*, trying the next A record when one fails, while keeping the
  per-request timeout intact.
* TLS verification is never weakened and SNI/certificate checking is untouched
  (httpcore derives ``server_hostname`` from the request origin).
* No A record / unsupported httpx internals degrade to the stock SDK transport
  instead of failing the request.
* Nothing is monkeypatched process-wide: Telegram polling, MongoDB, Edge-TTS and
  plain HTTP calls keep the stock resolver.

No network, no Mongo, no ffmpeg.
"""

from __future__ import annotations

import socket
import ssl
import unittest
from unittest.mock import patch

import quizbot.runner_bot.handlers.podcast as pod

KEY_A = "AIzaSyTESTKEYAAAAAAA11111111111111111111"
HOST = "generativelanguage.googleapis.com"

try:  # production dependency; the SDK-level assertions are skipped without it
    import httpcore
    import httpx
    from google import genai
    from google.genai import types as genai_types

    HAVE_STACK = True
except Exception:  # pragma: no cover - minimal test environments
    HAVE_STACK = False


def a_record(ip, port=443):
    """One getaddrinfo() row for an IPv4 address."""
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))


class IPv4TransportBase(unittest.TestCase):
    def setUp(self):
        self._backend_class = pod._IPV4_BACKEND_CLASS
        self._warned = pod._IPV4_TRANSPORT_WARNED
        pod._IPV4_BACKEND_CLASS = None
        pod._IPV4_TRANSPORT_WARNED = False

    def tearDown(self):
        pod._IPV4_BACKEND_CLASS = self._backend_class
        pod._IPV4_TRANSPORT_WARNED = self._warned


# ---------------------------------------------------------------------------
# The Gemini client keeps its timeout and gains an IPv4-only transport
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAVE_STACK, "google-genai/httpx not installed")
class ClientWiringCases(IPv4TransportBase):
    def test_client_keeps_timeout_and_gets_ipv4_transport(self):
        with patch.object(genai, "Client") as mock_client:
            pod._new_genai_client(KEY_A)

        opts = mock_client.call_args.kwargs["http_options"]
        self.assertEqual(opts.timeout, pod.GEMINI_HTTP_TIMEOUT_MS)
        self.assertEqual(pod.GEMINI_HTTP_TIMEOUT_MS, 150_000)
        transport = (opts.client_args or {}).get("transport")
        self.assertIsInstance(transport, httpx.HTTPTransport)
        backend = transport._pool._network_backend
        self.assertIsInstance(backend, pod._IPV4_BACKEND_CLASS)

    def test_real_sdk_adopts_the_ipv4_transport(self):
        """google-genai 2.x must actually use the transport we hand it."""
        client = pod._new_genai_client(KEY_A)
        http_client = client._api_client._httpx_client

        self.assertIs(http_client._transport._pool._network_backend.__class__,
                      pod._IPV4_BACKEND_CLASS)
        # The 150s bound still reaches the request layer (per-request timeout).
        self.assertEqual(client._api_client._http_options.timeout,
                         pod.GEMINI_HTTP_TIMEOUT_MS)

    def test_tls_verification_is_never_disabled(self):
        captured = {}
        real_transport = httpx.HTTPTransport

        def spy(*args, **kwargs):
            captured.update(kwargs)
            return real_transport(*args, **kwargs)

        with patch.object(httpx, "HTTPTransport", side_effect=spy):
            self.assertIsNotNone(pod._gemini_http_transport())

        verify = captured.get("verify", True)
        self.assertNotEqual(verify, False)
        self.assertTrue(verify is True or isinstance(verify, ssl.SSLContext))

    def test_stock_options_when_transport_unavailable(self):
        """Unsupported httpx internals must degrade, never break generation."""
        with patch.object(pod, "_gemini_http_transport", return_value=None), \
             patch.object(genai, "Client") as mock_client:
            pod._new_genai_client(KEY_A)

        opts = mock_client.call_args.kwargs["http_options"]
        self.assertEqual(opts.timeout, pod.GEMINI_HTTP_TIMEOUT_MS)
        self.assertFalse(opts.client_args)

    def test_degraded_dict_path_keeps_timeout_and_transport(self):
        transport = object()
        with patch.object(genai_types, "HttpOptions",
                          side_effect=RuntimeError("no typed helper")), \
             patch.object(pod, "_gemini_http_transport", return_value=transport), \
             patch.object(genai, "Client") as mock_client:
            pod._new_genai_client(KEY_A)

        opts = mock_client.call_args.kwargs["http_options"]
        self.assertIsInstance(opts, dict)
        self.assertEqual(opts["timeout"], pod.GEMINI_HTTP_TIMEOUT_MS)
        self.assertEqual(opts["client_args"], {"transport": transport})


# ---------------------------------------------------------------------------
# Resolution: AF_INET only, never a AAAA query
# ---------------------------------------------------------------------------

class ResolutionCases(IPv4TransportBase):
    def test_getaddrinfo_is_called_with_af_inet_only(self):
        calls = []

        def fake_gai(host, port, family=0, type=0, *a, **k):
            calls.append((host, family))
            return [a_record("142.250.0.1", port), a_record("142.250.0.2", port)]

        with patch.object(pod.socket, "getaddrinfo", fake_gai):
            addrs = pod._ipv4_addresses(HOST, 443)

        self.assertEqual(addrs, ["142.250.0.1", "142.250.0.2"])
        self.assertEqual(calls, [(HOST, socket.AF_INET)])

    def test_duplicate_addresses_are_collapsed(self):
        with patch.object(pod.socket, "getaddrinfo",
                          lambda *a, **k: [a_record("1.2.3.4"), a_record("1.2.3.4")]):
            self.assertEqual(pod._ipv4_addresses(HOST, 443), ["1.2.3.4"])

    def test_dns_failure_returns_empty_not_raise(self):
        def boom(*a, **k):
            raise socket.gaierror("Name or service not known")

        with patch.object(pod.socket, "getaddrinfo", boom):
            self.assertEqual(pod._ipv4_addresses(HOST, 443), [])


# ---------------------------------------------------------------------------
# Dialing: IPv4 literals, per-address fallback, timeout preserved
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAVE_STACK, "httpcore not installed")
class DialingCases(IPv4TransportBase):
    def _backend(self):
        backend = pod._ipv4_network_backend()
        self.assertIsNotNone(backend)
        return backend

    def test_dials_ipv4_literal_with_timeout_intact(self):
        dialed = []

        def stock(self, host, port, timeout=None, local_address=None,
                  socket_options=None):
            dialed.append((host, port, timeout))
            return "STREAM"

        with patch.object(pod.socket, "getaddrinfo",
                          lambda *a, **k: [a_record("142.250.0.9")]), \
             patch.object(httpcore.SyncBackend, "connect_tcp", stock):
            out = self._backend().connect_tcp(HOST, 443, timeout=150.0)

        self.assertEqual(out, "STREAM")
        self.assertEqual(dialed, [("142.250.0.9", 443, 150.0)])

    def test_tries_next_a_record_when_one_fails(self):
        dialed = []

        def stock(self, host, port, timeout=None, local_address=None,
                  socket_options=None):
            dialed.append(host)
            if host == "142.250.0.1":
                raise httpcore.ConnectError("unreachable")
            return "STREAM-2"

        with patch.object(pod.socket, "getaddrinfo",
                          lambda *a, **k: [a_record("142.250.0.1"),
                                           a_record("142.250.0.2")]), \
             patch.object(httpcore.SyncBackend, "connect_tcp", stock):
            out = self._backend().connect_tcp(HOST, 443, timeout=150.0)

        self.assertEqual(out, "STREAM-2")
        self.assertEqual(dialed, ["142.250.0.1", "142.250.0.2"])

    def test_reraises_last_error_when_every_a_record_fails(self):
        def stock(self, host, port, timeout=None, local_address=None,
                  socket_options=None):
            raise httpcore.ConnectTimeout("timed out on %s" % host)

        with patch.object(pod.socket, "getaddrinfo",
                          lambda *a, **k: [a_record("142.250.0.1")]), \
             patch.object(httpcore.SyncBackend, "connect_tcp", stock):
            with self.assertRaises(httpcore.ConnectTimeout):
                self._backend().connect_tcp(HOST, 443, timeout=150.0)

    def test_falls_back_to_hostname_without_a_records(self):
        """IPv6-only hosts must still work through the stock resolver."""
        dialed = []

        def stock(self, host, port, timeout=None, local_address=None,
                  socket_options=None):
            dialed.append(host)
            return "STREAM"

        with patch.object(pod.socket, "getaddrinfo", lambda *a, **k: []), \
             patch.object(httpcore.SyncBackend, "connect_tcp", stock):
            out = self._backend().connect_tcp(HOST, 443, timeout=150.0)

        self.assertEqual(out, "STREAM")
        self.assertEqual(dialed, [HOST])

    def test_backend_is_reusable_and_class_is_memoized(self):
        first = pod._ipv4_network_backend()
        cls = pod._IPV4_BACKEND_CLASS
        second = pod._ipv4_network_backend()
        self.assertIsNotNone(first)
        self.assertIs(pod._IPV4_BACKEND_CLASS, cls)
        self.assertIsNot(first, second)
        self.assertIsInstance(second, cls)


# ---------------------------------------------------------------------------
# Blast radius: nothing global is patched, other features untouched
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAVE_STACK, "google-genai/httpx not installed")
class NoCollateralDamageCases(IPv4TransportBase):
    def test_no_process_wide_socket_patching(self):
        gai_before, connect_before = socket.getaddrinfo, socket.socket.connect

        pod._new_genai_client(KEY_A)          # builds the transport + backend

        def stock(self, host, port, timeout=None, local_address=None,
                  socket_options=None):
            return "STREAM"

        with patch.object(socket, "getaddrinfo",
                          lambda *a, **k: [a_record("142.250.0.1")]), \
             patch.object(httpcore.SyncBackend, "connect_tcp", stock):
            pod._ipv4_network_backend().connect_tcp(HOST, 443, timeout=1.0)

        self.assertIs(socket.getaddrinfo, gai_before)
        self.assertIs(socket.socket.connect, connect_before)

    def test_error_classification_and_retry_knobs_unchanged(self):
        self.assertEqual(pod.GEMINI_HTTP_TIMEOUT_MS, 150_000)
        self.assertEqual(pod.GEMINI_CALL_TIMEOUT, 180.0)
        self.assertEqual(pod.GEMINI_MAX_RETRIES, 2)
        self.assertEqual(pod._RETRY_BACKOFF, (1.0, 3.0))
        self.assertEqual(
            pod._classify_gemini_error(Exception("400 API key not valid")).kind,
            "invalid_key")
        self.assertEqual(
            pod._classify_gemini_error(Exception("ConnectTimeout timed out")).kind,
            "transient")

    def test_models_and_voices_unchanged(self):
        self.assertEqual(pod.GEMINI_TEXT_MODEL, "gemini-2.5-flash")
        self.assertEqual(pod.GEMINI_TTS_MODEL, "gemini-2.5-flash-preview-tts")
        self.assertEqual(pod.TTS_VOICE_FEMALE, "Kore")
        self.assertEqual(pod.TTS_VOICE_MALE, "Puck")
        self.assertEqual(pod.BRAND, "Journey for लबासना")


if __name__ == "__main__":
    unittest.main()
