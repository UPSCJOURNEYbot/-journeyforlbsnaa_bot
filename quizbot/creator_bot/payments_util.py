"""Razorpay payment-link helpers."""
from __future__ import annotations

import base64
import logging
from typing import Optional

from quizbot.shared import config
from quizbot.shared.utils.http import request_json

logger = logging.getLogger(__name__)
RAZORPAY_API_BASE = "https://api.razorpay.com/v1"


def _auth_header() -> dict[str, str]:
    raw = f"{config.RAZORPAY_KEY_ID}:{config.RAZORPAY_KEY_SECRET}".encode()
    token = base64.b64encode(raw).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


async def create_payment_link(amount_inr: int, token: str, bot_username: str) -> Optional[dict]:
    """Create a Razorpay payment link. Amounts passed here are INR rupees."""
    if not config.RAZORPAY_KEY_ID or not config.RAZORPAY_KEY_SECRET:
        logger.warning("Razorpay credentials are not configured")
        return None
    payload = {
        "amount": amount_inr * 100,
        "currency": "INR",
        "description": "Quiz Bot Premium",
        "callback_url": f"https://t.me/{bot_username}?start=pay_{token}",
        "callback_method": "get",
        "reminder_enable": False,
    }
    try:
        status, body = await request_json(
            "POST", f"{RAZORPAY_API_BASE}/payment_links",
            headers=_auth_header(), json_body=payload,
        )
    except Exception:
        logger.exception("Razorpay payment-link request failed")
        return None
    if status not in (200, 201) or not isinstance(body, dict):
        logger.warning("Razorpay rejected payment-link request: %s %s", status, body)
        return None
    return body


async def fetch_payment_link(link_id: str) -> Optional[dict]:
    """Fetch current server-side status of a Razorpay payment link."""
    if not link_id or not config.RAZORPAY_KEY_ID or not config.RAZORPAY_KEY_SECRET:
        return None
    try:
        status, body = await request_json(
            "GET", f"{RAZORPAY_API_BASE}/payment_links/{link_id}",
            headers=_auth_header(),
        )
    except Exception:
        logger.exception("Razorpay payment-link lookup failed")
        return None
    if status != 200 or not isinstance(body, dict):
        logger.warning("Razorpay payment-link lookup rejected: %s %s", status, body)
        return None
    return body


def is_payment_link_paid(link: dict, expected_amount_inr: int) -> bool:
    """Require a fully paid link and the exact expected amount."""
    if link.get("status") != "paid":
        return False
    expected_paise = expected_amount_inr * 100
    try:
        amount = int(link.get("amount", -1))
        amount_paid = int(link.get("amount_paid", -1))
    except (TypeError, ValueError):
        return False
    return amount == expected_paise and amount_paid >= expected_paise


def calc_price(plan_key: str, qty: int) -> dict:
    """Bulk-discount price calculator. All returned prices are INR rupees."""
    plan = config.PLANS[plan_key]
    amount_per_unit = plan["amount"]
    days_per_unit = plan["days"]
    original = amount_per_unit * qty
    days = days_per_unit * qty
    if plan_key == "1_month":
        disc = 20 if original > 40000 else (10 if original > 20000 else 0)
    elif plan_key == "3_month":
        disc = 15 if original > 60000 else (5 if original > 30000 else 0)
    else:
        disc = 0
    price = round(original * (1 - disc / 100))
    return {"price": price, "original": original, "discount_pct": disc,
            "days": days, "label": plan["label"], "qty": qty}
