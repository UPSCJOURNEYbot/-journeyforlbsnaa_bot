"""
Regression tests for the production startup crash:

    TypeError: MotorCollection object is not callable
      at quizbot/analytics/gamification.ensure_gamification_indexes()
      ledger = db.collection("xp_ledger")

The app's ``Database`` wrapper exposes a ``.collection(name)`` convenience
method; a RAW ``AsyncIOMotorDatabase`` (what startup's ``Database.connect()``
hands to index bootstrap functions) does NOT — ``db.collection`` resolves, via
Motor's attribute access, to the collection literally named "collection",
which is not callable. Tests never contacted a real MongoDB server, so the
wrong-API fake double implementing ``.collection()`` as a method hid the bug.

These regressions run against the REAL Motor classes (no MongoDB server
needed — collection resolution and the index specs never touch the network
when ``create_index`` is intercepted).
"""

from __future__ import annotations

import ast
import pathlib
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

try:
    from motor.motor_asyncio import (
        AsyncIOMotorClient,
        AsyncIOMotorCollection,
    )
    HAS_MOTOR = True
except Exception:  # pragma: no cover
    HAS_MOTOR = False

import asyncio

from quizbot.analytics import gamification
from quizbot.database.db import Database


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@unittest.skipUnless(HAS_MOTOR, "motor not installed")
class RealMotorApiTests(unittest.TestCase):
    """Exercise code with REAL MotorDatabase semantics — no fake doubles."""

    def setUp(self):
        # No network is contacted while create_index is intercepted.
        self.client = AsyncIOMotorClient(
            "mongodb://127.0.0.1:1/", serverSelectionTimeoutMS=10)
        self.raw_db = self.client["quizbot"]
        self.calls = []

        async def fake_create_index(self, keys, **kwargs):
            self_calls = getattr(fake_create_index, "calls", None)
            self_calls.append((self.name, keys,
                               kwargs.get("name"), bool(kwargs.get("unique"))))

        fake_create_index.calls = self.calls
        self._patch = mock.patch.object(
            AsyncIOMotorCollection, "create_index", fake_create_index)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def tearDown(self):
        self.client.close()

    def test_raw_motor_collection_attribute_is_not_callable(self):
        # This IS the production error, reproduced on the real Motor class.
        with self.assertRaises(TypeError) as ctx:
            self.raw_db.collection("xp_ledger")
        self.assertIn("not callable", str(ctx.exception))

    def test_mapping_accessor_returns_same_collection(self):
        self.assertEqual(self.raw_db["xp_ledger"].name, "xp_ledger")
        self.assertEqual(self.raw_db.get_collection("xp_ledger").name,
                         "xp_ledger")

    def test_ensure_gamification_indexes_runs_on_raw_motor_db(self):
        # Exact startup call: Database._ensure_indexes passes the raw handle.
        _run(gamification.ensure_gamification_indexes(self.raw_db))
        by_coll = {}
        for coll, keys, name, unique in self.calls:
            by_coll.setdefault(coll, []).append((keys, name, unique))
        self.assertEqual(set(by_coll), {"xp_ledger", "user_xp"})
        ledger_names = [n for _, n, _ in by_coll["xp_ledger"]]
        self.assertEqual(
            ledger_names,
            ["uniq_xp_user_event", "xp_user_type_status",
             "xp_user_local_day", "xp_created_at"])
        # The identity index must remain unique (exactly-once XP awards).
        uniq = [x for x in by_coll["xp_ledger"] if x[1] == "uniq_xp_user_event"]
        self.assertEqual(len(uniq), 1)
        self.assertTrue(uniq[0][2])
        self.assertIn(("user_id", "uniq_user_xp_user", True),
                      [(k, n, u) for k, n, u in by_coll["user_xp"]])

    def test_gamification_service_accepts_raw_motor_db(self):
        svc = gamification.GamificationService(db=self.raw_db)
        self.assertIsInstance(svc.ledger, AsyncIOMotorCollection)
        self.assertEqual(svc.ledger.name, "xp_ledger")
        self.assertEqual(svc.users.name, "user_xp")

    def test_full_startup_index_bootstrap_completes_on_raw_motor_db(self):
        # The whole Database._ensure_indexes() (the crashing startup method)
        # runs to completion with a raw MotorDatabase and intercepted IO.
        holder = Database("mongodb://127.0.0.1:1/", "quizbot")
        holder._client = self.client
        holder._db = self.raw_db
        _run(holder._ensure_indexes())
        # gamification indexes are part of the startup run
        names = [n for _, _, n, _ in self.calls]
        self.assertIn("uniq_xp_user_event", names)
        self.assertIn("uniq_user_xp_user", names)


class WrapperMappingApiTests(unittest.TestCase):
    """The app wrapper supports BOTH .collection() and Motor mapping API."""

    def test_wrapper_mapping_and_collection_paths(self):
        class FakeRaw:
            def __getitem__(self, name):
                return ("coll", name)

            def get_collection(self, name, *a, **k):
                return ("get", name)

        d = Database("mongodb://x", "quizbot")
        d._db = FakeRaw()
        self.assertEqual(d["users"], ("coll", "users"))
        self.assertEqual(d.collection("users"), ("coll", "users"))
        self.assertEqual(d.get_collection("users"), ("get", "users"))

    def test_wrapper_db_property_requires_connect(self):
        d = Database("mongodb://x", "quizbot")
        with self.assertRaises(RuntimeError):
            d.collection("users")


class GamificationSourceAuditTests(unittest.TestCase):
    """No real ``db.collection(...)`` call may remain in gamification (the
    mapping API is the only spelling valid for both db handle types)."""

    def test_gamification_uses_mapping_api_only(self):
        src = (REPO_ROOT / "quizbot/analytics/gamification.py").read_text()
        tree = ast.parse(src)
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                # db.collection("x") / self.db.collection("x")
                if node.func.attr == "collection":
                    violations.append(node.lineno)
        self.assertEqual(violations, [],
                         f"db.collection(...) calls remain at lines {violations}")

    def test_every_index_bootstrap_uses_raw_motor_safe_resolution(self):
        """Any function that creates indexes may receive a RAW MotorDatabase
        during startup, so it must never spell ``.collection(...)``. This
        guards future bootstrap functions from repeating the B12 crash."""
        for py in (REPO_ROOT / "quizbot").rglob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                body_src = ast.get_source_segment(
                    py.read_text(encoding="utf-8"), node) or ""
                if "create_index" not in body_src:
                    continue
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Call)
                            and isinstance(sub.func, ast.Attribute)
                            and sub.func.attr == "collection"):
                        self.fail(
                            f"{py.relative_to(REPO_ROOT)}:{sub.lineno} index "
                            f"bootstrap {node.name}() calls .collection() — "
                            "invalid on a raw MotorDatabase; use db['name'] "
                            "or attribute access.")


if __name__ == "__main__":
    unittest.main()
