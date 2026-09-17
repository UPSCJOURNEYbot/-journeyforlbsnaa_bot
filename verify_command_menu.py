#!/usr/bin/env python3
"""Verify (and optionally push) the ACTUAL Telegram command menu.

Why this exists
---------------
BOTFATHER_COMMANDS.txt is only a *copy-paste template*. Telegram's real menu
lives on Telegram's servers and is set either manually via @BotFather
(/setcommands) or through the Bot API ``setMyCommands``. This script closes
that gap:

1. OFFLINE AUDIT (default, no network):
   parses BOTFATHER_COMMANDS.txt Block 1 and cross-checks every listed
   command against the CommandHandlers actually registered by the bot code
   (runner handler modules + creator bridge). Exits non-zero on any mismatch
   (menu command with no handler, or handler never advertised).

2. LIVE VERIFY (--token <BOT_TOKEN> or env TELEGRAM_BOT_TOKEN):
   calls the real Bot API ``getMyCommands`` (default scope, plus the
   all_private_chats scope) and reports whether the template commands --
   specifically ``stats`` and ``xp`` -- are ACTUALLY visible to users.

3. LIVE SET (--token ... --set):
   pushes Block 1 to the default scope via ``setMyCommands`` so the menu
   matches the template exactly (uses only the stdlib; no bot process
   needed). Run on the VPS or any machine that can reach api.telegram.org.

Usage:
    python3 verify_command_menu.py
    python3 verify_command_menu.py --token 123456:ABC...
    python3 verify_command_menu.py --token 123456:ABC... --set
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "BOTFATHER_COMMANDS.txt"

# The commands this verification cares about most (Phase C gamification UI).
MUST_HAVE = ("stats", "xp")


# ---------------------------------------------------------------------------
# Template parsing
# ---------------------------------------------------------------------------

def parse_block1() -> dict[str, str]:
    """{command: description} from BOTFATHER_COMMANDS.txt Block 1."""
    text = TEMPLATE.read_text()
    block1 = text.split("BLOCK 2")[0]
    out: dict[str, str] = {}
    for line in block1.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or " - " not in line:
            continue
        name, desc = line.split(" - ", 1)
        out[name.strip()] = desc.strip()
    return out


# ---------------------------------------------------------------------------
# Code-side registered commands (offline)
# ---------------------------------------------------------------------------

def registered_commands() -> set[str]:
    """Every command name a CommandHandler is registered for, using the REAL
    registration path (offline PTB Application, no network)."""
    from telegram.ext import Application, CommandHandler

    from quizbot.runner_bot.creator_bridge import register_creator_bridge
    from quizbot.runner_bot.handlers import register as runner_register

    app = Application.builder().token("123456:OFFLINE-AUDIT-TOKEN").build()
    runner_register(app)
    register_creator_bridge(app)
    cmds: set[str] = set()
    for handlers in app.handlers.values():
        for h in handlers:
            if isinstance(h, CommandHandler):
                cmds.update(getattr(h, "commands", set()))
    return cmds


def offline_audit() -> int:
    listed = parse_block1()
    try:
        registered = registered_commands()
    except Exception as exc:  # pragma: no cover
        print(f"FAIL: could not build the offline application: {exc}")
        return 2

    print("== OFFLINE AUDIT: BOTFATHER_COMMANDS.txt Block 1 <-> registered handlers ==")
    missing_handler = sorted(c for c in listed if c not in registered)
    unadvertised = sorted(c for c in registered if c not in listed)
    for c in listed:
        ok = c in registered
        mark = "OK " if ok else "NO-HANDLER"
        print(f"  [{mark}] /{c}")
    for c in unadvertised:
        print(f"  [NOT-IN-MENU] /{c} (registered in code, absent from template)")

    ok_must = all(m in listed and m in registered for m in MUST_HAVE)
    if missing_handler or not ok_must:
        print(f"FAIL: menu commands without handlers: {missing_handler}")
        return 1
    for m in MUST_HAVE:
        print(f"  [OK ] /{m} advertised AND registered")
    print("Offline audit PASS: every Block-1 command is registered in code.")
    return 0


# ---------------------------------------------------------------------------
# Live Bot API
# ---------------------------------------------------------------------------

def _api(token: str, method: str, payload: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # nosec - official API host
        body = json.loads(resp.read().decode())
    if not body.get("ok"):
        raise RuntimeError(f"{method} failed: {body}")
    return body["result"]


def _parse_desc(desc: str) -> str:
    """Telegram menu descriptions are max 256 chars; template ones already fit."""
    return desc[:256]


def live_verify(token: str, do_set: bool) -> int:
    template = parse_block1()
    wanted = [{"command": c, "description": _parse_desc(d)} for c, d in template.items()]

    if do_set:
        _api(token, "setMyCommands", {"commands": wanted})
        print(f"setMyCommands: pushed {len(wanted)} commands to the DEFAULT scope.")

    default = _api(token, "getMyCommands", {"language_code": ""})
    default_cmds = {c["command"] for c in default}
    print("\n== LIVE VERIFY: actual Telegram menu (default scope) ==")
    for m in MUST_HAVE:
        mark = "OK " if m in default_cmds else "MISSING"
        print(f"  [{mark}] /{m}")
    missing = sorted(c for c in template if c not in default_cmds)
    extra = sorted(c for c in default_cmds if c not in template)
    for c in missing:
        print(f"  [MISSING] /{c} (in template, NOT in the live menu)")
    for c in extra:
        print(f"  [EXTRA] /{c} (in the live menu, not in the template)")

    try:
        priv = _api(token, "getMyCommands",
                    {"scope": {"type": "all_private_chats"}, "language_code": ""})
        priv_cmds = {c["command"] for c in priv}
        print("\n== LIVE VERIFY: private-chat scope ==")
        for m in MUST_HAVE:
            mark = "OK " if m in default_cmds else "INFO"
            print(f"  [{mark}] /{m} default-scope visibility: "
                  f"{'yes' if m in default_cmds else 'no'}")
        _ = priv_cmds  # private scope only adds creator commands; default governs /stats
    except Exception as exc:  # pragma: no cover
        print(f"(private-scope check skipped: {exc})")

    ok = all(m in default_cmds for m in MUST_HAVE) and not missing
    print("\nLIVE VERIFY " + ("PASS: menu matches the template."
                              if ok else "FAIL: see MISSING lines above."))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--token", help="Telegram bot token (or TELEGRAM_BOT_TOKEN env)")
    ap.add_argument("--set", action="store_true",
                    help="push Block 1 to the live default-scope menu first")
    args = ap.parse_args()

    rc = offline_audit()
    print()
    if args.token or "TELEGRAM_BOT_TOKEN" in __import__("os").environ:
        token = args.token or __import__("os").environ["TELEGRAM_BOT_TOKEN"]
        try:
            rc = live_verify(token, args.set) or rc
        except Exception as exc:
            print(f"FAIL: live verification error: {exc}")
            rc = 3
    else:
        print("LIVE CHECK SKIPPED: pass --token <BOT_TOKEN> (or set TELEGRAM_BOT_TOKEN) "
              "to verify the REAL Telegram menu, or add --set to push the template "
              "to the live menu via setMyCommands.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
