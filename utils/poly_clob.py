"""CLOB live execution stub — paper mode is default until POLY_LIVE=1.

Live path needs your own private key + CLOB API creds; funds stay in your
Polymarket account. Not wired until paper copy is verified.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def live_enabled() -> bool:
    raw = (os.getenv("POLY_LIVE") or "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def status() -> dict[str, Any]:
    return {
        "enabled": live_enabled(),
        "ready": False,
        "note": (
            "Live CLOB execution is disabled until paper copy is confirmed. "
            "Set POLY_LIVE=1 and configure POLY_PRIVATE_KEY / funder / signature_type "
            "when ready."
        ),
        "has_private_key": bool((os.getenv("POLY_PRIVATE_KEY") or "").strip()),
        "funder": (os.getenv("POLY_FUNDER_ADDRESS") or "").strip() or None,
        "signature_type": (os.getenv("POLY_SIGNATURE_TYPE") or "").strip() or None,
    }


def place_copy_order(fill: dict[str, Any]) -> dict[str, Any]:
    """Would post a market order mirroring ``fill``. Paper-only for now."""
    if not live_enabled():
        return {"ok": False, "reason": "live_disabled"}
    logger.warning("poly CLOB live place_copy_order not implemented: %s", fill.get("dedupe_key"))
    return {"ok": False, "reason": "not_implemented"}
