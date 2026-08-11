"""HL → Binance sub-account routing (parallel to Bitget subs).

Each entry binds one paper bot to one Binance API key set
({env_prefix}_API_KEY / _API_SECRET). Main BINANCE_* stays for ORB/vnpy.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from utils.hl_short_term import PROJECT_ROOT, resolve_data_dir

logger = logging.getLogger(__name__)

CONFIG_NAME = "hl_binance_subaccounts.json"
DEFAULT_MAX_SUBACCOUNTS = 8

_lock = threading.Lock()
_cache: dict[str, Any] | None = None
_cache_mtime: float | None = None


@dataclass(frozen=True)
class BinanceSubRoute:
    id: str
    label: str
    bot_id: str
    coins: frozenset[str] | None
    enabled: bool
    env_prefix: str
    scale: float

    def allows_coin(self, coin: str) -> bool:
        if self.coins is None:
            return True
        from utils.hl_binance_symbol_map import hl_base_ticker

        base = hl_base_ticker(coin)
        if not base:
            return False
        allowed = {hl_base_ticker(c) or str(c).strip().upper() for c in self.coins}
        return base in allowed


def max_subaccounts() -> int:
    try:
        n = int(
            os.getenv("HL_BINANCE_MAX_SUBACCOUNTS", str(DEFAULT_MAX_SUBACCOUNTS))
            or DEFAULT_MAX_SUBACCOUNTS
        )
    except (TypeError, ValueError):
        n = DEFAULT_MAX_SUBACCOUNTS
    return max(1, min(32, n))


def _config_path() -> Path:
    root = PROJECT_ROOT / CONFIG_NAME
    if root.is_file():
        return root
    return resolve_data_dir() / CONFIG_NAME


def invalidate_cache() -> None:
    global _cache, _cache_mtime
    with _lock:
        _cache = None
        _cache_mtime = None


def load_subaccounts_doc(*, force: bool = False) -> dict[str, Any]:
    global _cache, _cache_mtime
    path = _config_path()
    with _lock:
        try:
            mtime = path.stat().st_mtime if path.exists() else None
        except OSError:
            mtime = None
        if not force and _cache is not None and mtime == _cache_mtime:
            return _cache
        if not path.exists():
            doc = {"updated": None, "subaccounts": [], "error": f"missing: {path}"}
            _cache = doc
            _cache_mtime = mtime
            return doc
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                doc = {"subaccounts": [], "error": "invalid root"}
        except Exception as exc:
            logger.warning("hl_binance_subaccounts load failed: %s", exc)
            doc = {"subaccounts": [], "error": str(exc)}
        _cache = doc
        _cache_mtime = mtime
        return doc


def parse_routes(doc: dict[str, Any] | None = None) -> list[BinanceSubRoute]:
    raw = doc if doc is not None else load_subaccounts_doc()
    out: list[BinanceSubRoute] = []
    seen: set[str] = set()
    for row in raw.get("subaccounts") or []:
        if not isinstance(row, dict):
            continue
        rid = str(row.get("id") or "").strip()
        bot_id = str(row.get("bot_id") or "").strip()
        if not rid or not bot_id or rid in seen:
            continue
        seen.add(rid)
        coins_raw = row.get("coins")
        if coins_raw is None or coins_raw == [] or coins_raw == "*":
            coins = None
        elif isinstance(coins_raw, str):
            coins = frozenset(c.strip().upper() for c in coins_raw.split(",") if c.strip())
        else:
            coins = frozenset(str(c).strip().upper() for c in coins_raw if str(c).strip()) or None
        try:
            scale = max(0.0, float(row.get("scale", 1) or 1))
        except (TypeError, ValueError):
            scale = 1.0
        # Railway override: HL_BINANCE_SUB_<ID>_SCALE=0.5
        env_sc = os.getenv(f"HL_BINANCE_SUB_{rid.upper()}_SCALE", "").strip()
        if env_sc:
            try:
                scale = max(0.0, float(env_sc))
            except (TypeError, ValueError):
                pass
        enabled = bool(row.get("enabled", False))
        env_en = os.getenv(f"HL_BINANCE_SUB_{rid.upper()}_ENABLED", "").strip().lower()
        if env_en in ("1", "true", "yes", "on"):
            enabled = True
        elif env_en in ("0", "false", "no", "off"):
            enabled = False
        out.append(
            BinanceSubRoute(
                id=rid,
                label=str(row.get("label") or rid).strip(),
                bot_id=bot_id,
                coins=coins,
                enabled=enabled,
                env_prefix=str(row.get("env_prefix") or f"BINANCE_SUB_{rid.upper()}").strip(),
                scale=scale,
            )
        )
    return out


def enabled_routes() -> list[BinanceSubRoute]:
    enabled = [r for r in parse_routes() if r.enabled]
    cap = max_subaccounts()
    if len(enabled) > cap:
        logger.error(
            "enabled binance subaccounts %d > max %d — refusing all",
            len(enabled),
            cap,
        )
        return []
    return enabled


def routes_for_bot(bot_id: str) -> list[BinanceSubRoute]:
    bid = str(bot_id or "").strip()
    return [r for r in enabled_routes() if r.bot_id == bid]


def validate_routes(routes: list[BinanceSubRoute] | None = None) -> list[str]:
    routes = routes if routes is not None else parse_routes()
    problems: list[str] = []
    enabled = [r for r in routes if r.enabled]
    if len(enabled) > max_subaccounts():
        problems.append(f"enabled binance subs {len(enabled)} > max {max_subaccounts()}")
    seen_bots: dict[str, str] = {}
    for r in enabled:
        if r.bot_id in seen_bots and seen_bots[r.bot_id] != r.id:
            problems.append(f"bot {r.bot_id} mapped to both {seen_bots[r.bot_id]} and {r.id}")
        seen_bots[r.bot_id] = r.id
        if not r.env_prefix:
            problems.append(f"sub {r.id} missing env_prefix")
    return problems
