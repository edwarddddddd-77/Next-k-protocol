"""Polymarket watchlist + data-dir helpers."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WATCHLIST_NAME = "poly_watchlist.json"
DEFAULT_PAPER_BALANCE = 5000.0
_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def resolve_data_dir() -> Path:
    raw = (os.getenv("DATA_DIR") or "").strip()
    if raw:
        root = Path(raw).expanduser()
    else:
        for candidate in (Path("/app/data"), Path("/data")):
            if candidate.is_dir():
                root = candidate
                break
        else:
            root = PROJECT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    return root


def _watchlist_path() -> Path:
    """Prefer repo / POLY_WATCHLIST_PATH so deploys override a stale DATA_DIR copy."""
    env = (os.getenv("POLY_WATCHLIST_PATH") or "").strip()
    if env:
        return Path(env).expanduser()
    root = PROJECT_ROOT / WATCHLIST_NAME
    if root.is_file():
        return root
    data = resolve_data_dir() / WATCHLIST_NAME
    if data.is_file():
        return data
    return root


def normalize_address(raw: Any) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    if not s.startswith("0x"):
        s = "0x" + s
    if not _ADDR_RE.match(s):
        return ""
    return s.lower()


def load_watchlist_doc() -> dict[str, Any]:
    path = _watchlist_path()
    if not path.is_file():
        return {"updated": None, "venue": "polymarket", "wallets": [], "path": str(path)}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        doc = {"wallets": []}
    if not isinstance(doc, dict):
        doc = {"wallets": []}
    doc["path"] = str(path)
    return doc


def load_watchlist() -> list[dict[str, Any]]:
    doc = load_watchlist_doc()
    wallets = doc.get("wallets") or []
    if not isinstance(wallets, list):
        return []

    env_addr = normalize_address(os.getenv("POLY_TARGET_WALLET"))
    any_file_addr = any(
        normalize_address(x.get("address")) for x in wallets if isinstance(x, dict)
    )
    out: list[dict[str, Any]] = []
    for i, w in enumerate(wallets):
        if not isinstance(w, dict):
            continue
        enabled = w.get("enabled", True)
        if enabled is False or str(enabled).strip().lower() in ("0", "false", "no", "off"):
            continue
        addr = normalize_address(w.get("address"))
        # Env fills empty address when the file has no addresses at all, or
        # only on the first seat when that seat's address is blank.
        if not addr and env_addr and (not any_file_addr or i == 0):
            addr = env_addr
        if not addr:
            continue
        row = dict(w)
        row["address"] = addr
        row["id"] = str(w.get("id") or f"bot_{addr[:8]}").strip() or f"bot_{addr[:8]}"
        row["paper_balance"] = float(
            w["paper_balance"]
            if w.get("paper_balance") is not None
            else (os.getenv("POLY_PAPER_BALANCE") or DEFAULT_PAPER_BALANCE)
        )
        # coalesce_sec: merge same-market same-side burst into one copy.
        # 0 = copy every distinct tx immediately. debounce_sec is legacy alias.
        coalesce = w.get("coalesce_sec")
        if coalesce is None:
            env_c = os.getenv("POLY_COALESCE_SEC")
            coalesce = env_c if env_c is not None and str(env_c).strip() != "" else None
        if coalesce is None:
            coalesce = w.get("debounce_sec")
        if coalesce is None:
            env_d = os.getenv("POLY_DEBOUNCE_SEC")
            coalesce = env_d if env_d is not None and str(env_d).strip() != "" else 2
        row["coalesce_sec"] = float(coalesce)
        row["debounce_sec"] = 0.0
        row["copy_current"] = _truthy(w.get("copy_current"))
        row["paper"] = _truthy(w.get("paper", True))
        row["live"] = _truthy(w.get("live", False))
        out.append(row)

    if not out and env_addr:
        out.append(
            {
                "id": "bot_poly",
                "address": env_addr,
                "paper_balance": float(
                    os.getenv("POLY_PAPER_BALANCE") or DEFAULT_PAPER_BALANCE
                ),
                "coalesce_sec": float(
                    os.getenv("POLY_COALESCE_SEC")
                    if os.getenv("POLY_COALESCE_SEC") not in (None, "")
                    else (
                        os.getenv("POLY_DEBOUNCE_SEC")
                        if os.getenv("POLY_DEBOUNCE_SEC") not in (None, "")
                        else 2
                    )
                ),
                "debounce_sec": 0.0,
                "copy_current": False,
                "paper": True,
                "live": False,
            }
        )
    return out


def _truthy(raw: Any) -> bool:
    if raw is True:
        return True
    if raw is False or raw is None:
        return False
    return str(raw).strip().lower() in ("1", "true", "yes", "on")
