"""Desk consensus paper — Purffle-style rules, WS position book.

Mirrors Chamanrajragu/purffle-copybot consensus logic:
  long-only, ≥MIN_AGREE elites long → open, long_count < MIN_AGREE → exit,
  hard-stop −10%, ~20% of cash, 0.1% fee/side.
Difference: elite positions come from the desk WS book (+ REST seed/reconcile)
instead of a 5-minute REST poll; execution marks use HL mid (paper).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.hl_paper_copy import fetch_all_mids
from utils.hl_short_term import is_hl_spot_coin, load_watchlist, snapshot_positions
from utils.rate_limit import MinIntervalGuard

logger = logging.getLogger(__name__)

LEDGER_NAME = "hl_consensus_paper.json"
_lock = threading.Lock()
_book_lock = threading.Lock()
_tick_run_lock = threading.Lock()
_tick_guard = MinIntervalGuard("HL_CONSENSUS_TICK_SEC", 90.0)
_fill_guard = MinIntervalGuard("HL_CONSENSUS_FILL_TICK_SEC", 15.0)
_reconcile_guard = MinIntervalGuard("HL_CONSENSUS_RECONCILE_SEC", 600.0)

# addr -> {id, av, positions: {coin: {szi, entry, px, ntl}}, source, at, tids}
_wallet_book: dict[str, dict[str, Any]] = {}


def _data_dir() -> Path:
    raw = (os.getenv("DATA_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser()
    for candidate in (Path("/app/data"), Path("/data")):
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parents[1]


def _path() -> Path:
    return _data_dir() / LEDGER_NAME


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)).strip() or default)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(float(os.getenv(key, str(default)).strip() or default))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = (os.getenv(key) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def consensus_enabled() -> bool:
    return _env_bool("HL_CONSENSUS_ENABLED", False)


def consensus_config() -> dict[str, Any]:
    """Defaults match purffle_copytrade.py (WS book instead of 5m poll)."""
    coins_raw = (os.getenv("HL_CONSENSUS_COINS") or "").strip()
    allow = (
        {c.strip().upper() for c in coins_raw.split(",") if c.strip()}
        if coins_raw
        else None
    )
    # Fee: Purffle SPOT_FEE=0.001 → 10 bps/side. Prefer FEE_RATE, else FEE_BPS.
    fee_rate = _env_float("HL_CONSENSUS_FEE_RATE", -1.0)
    if fee_rate < 0:
        fee_rate = _env_float("HL_CONSENSUS_FEE_BPS", 10.0) / 10_000.0
    return {
        "enabled": consensus_enabled(),
        "mode": "purffle_long_ws",
        "balance": _env_float("HL_CONSENSUS_BALANCE", 5000.0),
        "min_agree": max(2, _env_int("HL_CONSENSUS_MIN_AGREE", 2)),
        "position_pct": min(0.5, max(0.05, _env_float("HL_CONSENSUS_POS_PCT", 0.20))),
        "leverage": 1.0,
        "max_positions": max(1, _env_int("HL_CONSENSUS_MAX_POS", 5)),
        "hard_stop_pct": max(0.02, _env_float("HL_CONSENSUS_HARD_STOP", 0.10)),
        # Purffle counts any open long; keep a tiny floor to drop dust.
        "min_vote_ntl": max(0.0, _env_float("HL_CONSENSUS_MIN_VOTE_NTL", 1.0)),
        "min_our_ntl": max(5.0, _env_float("HL_CONSENSUS_MIN_NTL", 5.0)),
        "allow_xyz": _env_bool("HL_CONSENSUS_ALLOW_XYZ", False),
        "allow_coins": sorted(allow) if allow else None,
        "tick_sec": max(30.0, _env_float("HL_CONSENSUS_TICK_SEC", 90.0)),
        "fill_tick_sec": max(5.0, _env_float("HL_CONSENSUS_FILL_TICK_SEC", 15.0)),
        "reconcile_sec": max(60.0, _env_float("HL_CONSENSUS_RECONCILE_SEC", 600.0)),
        "fee_rate": max(0.0, fee_rate),
        "fee_bps": round(max(0.0, fee_rate) * 10_000.0, 4),
        "long_only": True,
        "pool": "watchlist",
        "venue": "hl_paper_mid",
        "book": "ws_cache+rest_reconcile",
        "rules": "purffle_copybot",
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _signed_sz(fill: dict[str, Any]) -> float | None:
    try:
        sz = abs(float(fill.get("sz") or 0))
    except (TypeError, ValueError):
        return None
    if sz <= 0:
        return None
    side = str(fill.get("side") or "").strip().upper()
    if side in ("B", "BUY"):
        return sz
    if side in ("A", "SELL"):
        return -sz
    direction = str(fill.get("dir") or "").strip().lower()
    if "open long" in direction or "close short" in direction:
        return sz
    if "open short" in direction or "close long" in direction:
        return -sz
    return None


def _upsert_pos(
    positions: dict[str, dict[str, Any]],
    coin: str,
    *,
    start: float,
    end: float,
    px: float,
) -> None:
    if abs(end) < 1e-12:
        positions.pop(coin, None)
        return
    cur = positions.get(coin) or {}
    entry = float(cur.get("entry") or 0)
    if abs(start) < 1e-12 and abs(end) > 1e-12:
        entry = px
    elif start * end < 0:
        entry = px
    elif abs(end) > abs(start) + 1e-12 and px > 0:
        added = abs(end) - abs(start)
        if entry > 0 and added > 0:
            entry = (entry * abs(start) + px * added) / abs(end)
        else:
            entry = px
    elif entry <= 0 and px > 0:
        entry = px
    mark = px if px > 0 else entry
    positions[coin] = {
        "szi": end,
        "entry": entry,
        "px": mark,
        "ntl": abs(end) * (entry if entry > 0 else mark),
    }


def seed_wallet_from_rest(
    address: str,
    bot_id: str | None = None,
    *,
    force: bool = False,
) -> bool:
    """REST snapshot → wallet book (subscribe / reconcile).

    Skips overwrite when a fresh WS book exists (unless force), so reconcile
    does not clobber fills that just landed.
    """
    addr = (address or "").strip().lower()
    if not addr.startswith("0x"):
        return False
    bid = (bot_id or addr[:10]).strip()
    protect_sec = max(5.0, _env_float("HL_CONSENSUS_WS_PROTECT_SEC", 45.0))
    if not force:
        with _book_lock:
            cur = _wallet_book.get(addr)
            if (
                cur
                and cur.get("ok")
                and cur.get("source") == "ws"
                and (time.time() - float(cur.get("at") or 0)) < protect_sec
            ):
                return True
    try:
        snap = snapshot_positions(addr)
    except Exception as exc:  # noqa: BLE001
        logger.debug("consensus seed %s: %s", addr[:10], exc)
        with _book_lock:
            row = _wallet_book.get(addr) or {"id": bid, "positions": {}, "tids": []}
            row.update(
                {"id": bid, "ok": False, "error": str(exc)[:120], "at": time.time()}
            )
            _wallet_book[addr] = row
        return False
    positions: dict[str, dict[str, Any]] = {}
    for p in snap.get("positions") or []:
        coin = str(p.get("coin") or "").strip()
        if not coin or is_hl_spot_coin(coin):
            continue
        try:
            szi = float(p.get("szi") or 0)
            entry = float(p.get("entry") or 0)
        except (TypeError, ValueError):
            continue
        if abs(szi) < 1e-12:
            continue
        px = entry if entry > 0 else 0.0
        positions[coin] = {
            "szi": szi,
            "entry": entry,
            "px": px,
            "ntl": abs(szi) * entry if entry > 0 else 0.0,
            "uPnl": p.get("uPnl"),
        }
    with _book_lock:
        # Re-check WS protect after REST round-trip
        if not force:
            cur = _wallet_book.get(addr)
            if (
                cur
                and cur.get("ok")
                and cur.get("source") == "ws"
                and (time.time() - float(cur.get("at") or 0)) < protect_sec
            ):
                return True
        prev = _wallet_book.get(addr) or {}
        _wallet_book[addr] = {
            "id": bid,
            "address": addr,
            "av": float(snap.get("account_value") or 0),
            "positions": positions,
            "ok": True,
            "error": None,
            "source": "rest",
            "at": time.time(),
            "tids": list(prev.get("tids") or [])[-200:],
        }
    return True


def drop_wallet_book(address: str) -> None:
    addr = (address or "").strip().lower()
    with _book_lock:
        _wallet_book.pop(addr, None)


def apply_ws_fills(
    address: str,
    fills: list[Any],
    *,
    bot_id: str | None = None,
    is_snapshot: bool = False,
) -> int:
    """Apply live userFills into the wallet book. Snapshots trigger REST reseed."""
    addr = (address or "").strip().lower()
    if not addr.startswith("0x") or not isinstance(fills, list):
        return 0
    bid = (bot_id or addr[:10]).strip()
    if is_snapshot:
        seed_wallet_from_rest(addr, bid, force=True)
        return 0

    # Empty book → REST seed first so startPosition-less fills don't invent size.
    with _book_lock:
        missing = addr not in _wallet_book or not (_wallet_book.get(addr) or {}).get("ok")
    if missing:
        seed_wallet_from_rest(addr, bid, force=True)

    applied = 0
    with _book_lock:
        row = _wallet_book.get(addr) or {
            "id": bid,
            "address": addr,
            "av": 0.0,
            "positions": {},
            "ok": True,
            "source": "ws",
            "at": time.time(),
            "tids": [],
        }
        positions: dict[str, dict[str, Any]] = dict(row.get("positions") or {})
        tids: list[str] = list(row.get("tids") or [])
        seen = set(tids)

        for f in fills:
            if not isinstance(f, dict):
                continue
            coin = str(f.get("coin") or "").strip()
            if not coin or is_hl_spot_coin(coin):
                continue
            tid = str(f.get("tid") or f.get("hash") or "").strip()
            if tid and tid in seen:
                continue
            signed = _signed_sz(f)
            if signed is None:
                continue
            try:
                px = float(f.get("px") or 0)
            except (TypeError, ValueError):
                px = 0.0
            start_raw = f.get("startPosition")
            try:
                start = float(start_raw) if start_raw is not None and start_raw != "" else None
            except (TypeError, ValueError):
                start = None
            if start is None:
                cur = positions.get(coin) or {}
                start = float(cur.get("szi") or 0)
            end = start + signed
            _upsert_pos(positions, coin, start=start, end=end, px=px)
            if tid:
                seen.add(tid)
                tids.append(tid)
            applied += 1

        row.update(
            {
                "id": bid,
                "address": addr,
                "positions": positions,
                "ok": True,
                "error": None,
                "source": "ws" if applied else row.get("source") or "ws",
                "at": time.time(),
                "tids": tids[-200:],
            }
        )
        _wallet_book[addr] = row
    return applied


def reconcile_wallet_books(*, force: bool = False) -> int:
    """Periodic REST refresh for all watchlist wallets."""
    if not force:
        allowed, _ = _reconcile_guard.check_allow()
        if not allowed:
            return 0
    n = 0
    for w in load_watchlist():
        addr = str(w.get("address") or "").strip()
        bid = str(w.get("id") or "")
        # force=False respects WS protect window
        if seed_wallet_from_rest(addr, bid or None, force=force):
            n += 1
    _reconcile_guard.mark_used()
    return n


def book_status() -> dict[str, Any]:
    with _book_lock:
        rows = list(_wallet_book.values())
    return {
        "wallets": len(rows),
        "ok": sum(1 for r in rows if r.get("ok")),
        "ws": sum(1 for r in rows if r.get("source") == "ws"),
        "rest": sum(1 for r in rows if r.get("source") == "rest"),
    }


def _empty_ledger(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or consensus_config()
    bal = float(cfg["balance"])
    return {
        "updated_at": _now_iso(),
        "mode": "purffle_long_ws",
        "config": cfg,
        "cash": round(bal, 4),
        "equity": round(bal, 4),
        "init_balance": round(bal, 4),
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "positions": {},
        "fills": [],
        "signals": {},
        "fees_paid": 0.0,
        "elites": [],
        "last_tick_at": None,
        "last_tick_note": None,
        "last_tick_reason": None,
        "book": book_status(),
        "stats": {"opens": 0, "closes": 0, "stops": 0},
    }


def load_consensus() -> dict[str, Any]:
    with _lock:
        p = _path()
        if not p.exists():
            data = _empty_ledger()
            _write(data)
            return data
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("consensus ledger load failed: %s", exc)
            data = _empty_ledger()
        data["config"] = consensus_config()
        data["book"] = book_status()
        return data


def _write(data: dict[str, Any]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    data["updated_at"] = _now_iso()
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def save_consensus(data: dict[str, Any]) -> None:
    with _lock:
        _write(data)


def reset_consensus() -> dict[str, Any]:
    data = _empty_ledger()
    save_consensus(data)
    return data


def _coin_allowed(coin: str, cfg: dict[str, Any]) -> bool:
    c = str(coin or "").strip()
    if not c or is_hl_spot_coin(c):
        return False
    if c.startswith("@") or "/" in c:
        return False
    if not cfg.get("allow_xyz") and c.upper().startswith("XYZ:"):
        return False
    allow = cfg.get("allow_coins")
    if allow and c.upper() not in {a.upper() for a in allow}:
        return False
    return True


def _mid(mids: dict[str, float], coin: str) -> float:
    c = str(coin or "")
    for k in (c, c.upper(), c.replace("xyz:", "XYZ:")):
        try:
            px = float(mids.get(k) or 0)
        except (TypeError, ValueError):
            px = 0.0
        if px > 0:
            return px
    return 0.0


def _mark_positions(data: dict[str, Any], mids: dict[str, float]) -> None:
    """Purffle-style: equity = free cash + mark-to-mid holdings."""
    holdings = 0.0
    unreal = 0.0
    for coin, pos in list((data.get("positions") or {}).items()):
        px = _mid(mids, coin) or float(pos.get("mark") or pos.get("entry") or 0)
        if px <= 0:
            continue
        sz = float(pos.get("sz") or 0)
        entry = float(pos.get("entry") or px)
        cost = float(pos.get("cost") or abs(sz) * entry)
        mtm = abs(sz) * px
        upnl = mtm - cost
        pos["mark"] = round(px, 8)
        pos["upnl"] = round(upnl, 4)
        pos["ntl"] = round(mtm, 4)
        holdings += mtm
        unreal += upnl
    cash = float(data.get("cash") or 0)
    data["unrealized_pnl"] = round(unreal, 4)
    data["equity"] = round(cash + holdings, 4)


def _append_fill(data: dict[str, Any], fill: dict[str, Any]) -> None:
    fills = data.setdefault("fills", [])
    fills.append(fill)
    if len(fills) > 500:
        data["fills"] = fills[-500:]


def _close_pos(
    data: dict[str, Any],
    coin: str,
    px: float,
    *,
    reason: str,
    fee_rate: float = 0.0,
) -> None:
    pos = (data.get("positions") or {}).pop(coin, None)
    if not pos:
        return
    sz = abs(float(pos.get("sz") or 0))
    cost = float(pos.get("cost") or 0)
    if sz <= 0 or px <= 0:
        return
    proceeds = sz * px
    fee = proceeds * fee_rate
    realized = (proceeds - fee) - cost
    data["cash"] = round(float(data.get("cash") or 0) + proceeds - fee, 4)
    data["realized_pnl"] = round(float(data.get("realized_pnl") or 0) + realized, 4)
    data["fees_paid"] = round(float(data.get("fees_paid") or 0) + fee, 4)
    stats = data.setdefault("stats", {})
    stats["closes"] = int(stats.get("closes") or 0) + 1
    if reason == "hard_stop":
        stats["stops"] = int(stats.get("stops") or 0) + 1
    _append_fill(
        data,
        {
            "id": str(uuid.uuid4())[:8],
            "ts": _now_iso(),
            "coin": coin,
            "side": "sell",
            "sz": round(sz, 8),
            "px": px,
            "pnl": round(realized, 4),
            "fee": round(fee, 4),
            "reason": reason,
            "action": "close",
        },
    )


def _open_long(
    data: dict[str, Any],
    coin: str,
    px: float,
    spend: float,
    *,
    voters: list[str],
    agree: int,
    fee_rate: float = 0.0,
) -> bool:
    """Purffle open: spend fraction of cash, fee from spend, long-only."""
    if px <= 0 or spend <= 0:
        return False
    cash = float(data.get("cash") or 0)
    if spend > cash:
        spend = cash
    if spend < float((data.get("config") or {}).get("min_our_ntl") or 5):
        return False
    fee = spend * fee_rate
    qty = (spend - fee) / px
    if qty <= 0:
        return False
    cost = spend - fee
    data["cash"] = round(cash - spend, 4)
    data["fees_paid"] = round(float(data.get("fees_paid") or 0) + fee, 4)
    data.setdefault("positions", {})[coin] = {
        "coin": coin,
        "side": "long",
        "sz": round(qty, 8),
        "entry": px,
        "mark": px,
        "ntl": round(qty * px, 4),
        "cost": round(cost, 4),
        "upnl": 0.0,
        "opened_at": _now_iso(),
        "voters": voters,
        "agree": agree,
    }
    stats = data.setdefault("stats", {})
    stats["opens"] = int(stats.get("opens") or 0) + 1
    _append_fill(
        data,
        {
            "id": str(uuid.uuid4())[:8],
            "ts": _now_iso(),
            "coin": coin,
            "side": "buy",
            "sz": round(qty, 8),
            "px": px,
            "pnl": 0.0,
            "fee": round(fee, 4),
            "reason": f"copy_long_{agree}",
            "action": "open",
            "voters": voters,
        },
    )
    return True


def _scan_votes(
    cfg: dict[str, Any],
    *,
    use_cache: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Build long/short counts from WS book (Purffle aggregates longs)."""
    wallets = load_watchlist()
    elites: list[dict[str, Any]] = []
    votes: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "long": 0,
            "short": 0,
            "long_ids": [],
            "short_ids": [],
        }
    )
    min_ntl = float(cfg["min_vote_ntl"])

    for w in wallets:
        addr = str(w.get("address") or "").strip().lower()
        bid = str(w.get("id") or addr[:10])
        if not addr.startswith("0x"):
            continue
        with _book_lock:
            row = _wallet_book.get(addr)
        if (not use_cache) or (not row) or (not row.get("ok")):
            seed_wallet_from_rest(addr, bid)
            with _book_lock:
                row = _wallet_book.get(addr)
        if not row or not row.get("ok"):
            elites.append(
                {
                    "id": bid,
                    "address": addr,
                    "ok": False,
                    "error": (row or {}).get("error") or "no_book",
                    "positions": [],
                    "source": (row or {}).get("source"),
                }
            )
            continue
        pos_out: list[dict[str, Any]] = []
        for coin, p in (row.get("positions") or {}).items():
            if not _coin_allowed(coin, cfg):
                continue
            try:
                szi = float(p.get("szi") or 0)
                entry = float(p.get("entry") or p.get("px") or 0)
            except (TypeError, ValueError):
                continue
            if abs(szi) < 1e-12:
                continue
            ntl = abs(szi) * entry if entry > 0 else float(p.get("ntl") or 0)
            if ntl < min_ntl:
                continue
            side = "long" if szi > 0 else "short"
            votes[coin][side] += 1
            votes[coin][f"{side}_ids"].append(bid)
            pos_out.append(
                {
                    "coin": coin,
                    "side": side,
                    "szi": szi,
                    "ntl": round(ntl, 2),
                    "uPnl": p.get("uPnl"),
                }
            )
        elites.append(
            {
                "id": bid,
                "address": addr,
                "ok": True,
                "av": round(float(row.get("av") or 0), 2),
                "positions": pos_out,
                "source": row.get("source"),
                "book_age_sec": round(
                    max(0.0, time.time() - float(row.get("at") or 0)), 1
                ),
            }
        )
    return elites, dict(votes)


def tick_consensus(
    *,
    force: bool = False,
    reason: str = "periodic",
) -> dict[str, Any]:
    if not consensus_enabled():
        data = load_consensus()
        data["last_tick_note"] = "disabled"
        return data

    guard = _fill_guard if reason == "fill" else _tick_guard
    if not force:
        allowed, wait = guard.check_allow()
        if not allowed:
            data = load_consensus()
            data["tick_skipped"] = True
            data["retry_after_sec"] = round(wait, 1)
            return data

    if not _tick_run_lock.acquire(blocking=False):
        data = load_consensus()
        data["tick_skipped"] = True
        data["retry_after_sec"] = 1.0
        data["last_tick_note"] = "tick_busy"
        return data
    try:
        return _tick_consensus_body(force=force, reason=reason, guard=guard)
    finally:
        _tick_run_lock.release()


def _tick_consensus_body(
    *,
    force: bool,
    reason: str,
    guard: MinIntervalGuard,
) -> dict[str, Any]:
    """Purffle scan_and_mirror: long≥N open, long<N exit, hard-stop −10%."""
    cfg = consensus_config()
    with _lock:
        p = _path()
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                data = _empty_ledger(cfg)
        else:
            data = _empty_ledger(cfg)
        data["config"] = cfg
        data["mode"] = "purffle_long_ws"

    reconciled = 0
    if reason != "fill":
        reconciled = reconcile_wallet_books(force=force)

    elites, votes = _scan_votes(cfg, use_cache=True)
    try:
        mids = fetch_all_mids(force=(reason != "fill"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("consensus mids: %s", exc)
        mids = {}

    min_agree = int(cfg["min_agree"])
    fee_rate = float(cfg["fee_rate"])
    hard_stop = float(cfg["hard_stop_pct"])
    pos_pct = float(cfg["position_pct"])
    max_pos = int(cfg["max_positions"])
    min_spend = float(cfg["min_our_ntl"])

    signals: dict[str, Any] = {}
    for coin, v in votes.items():
        long_n = int(v["long"])
        short_n = int(v["short"])
        voters = list(v["long_ids"])
        side = "long" if long_n >= min_agree else None
        signals[coin] = {
            "long": long_n,
            "short": short_n,
            "side": side,
            "agree": long_n,
            "voters": voters if side else [],
        }

    data["elites"] = elites
    data["signals"] = signals
    data["book"] = book_status()
    _mark_positions(data, mids)

    # CLOSE: long_count < min_agree, or hard stop (Purffle order).
    for coin, pos in list((data.get("positions") or {}).items()):
        px = _mid(mids, coin) or float(pos.get("mark") or 0)
        if px <= 0:
            continue
        entry = float(pos.get("entry") or 0)
        if entry <= 0:
            continue
        long_n = int((signals.get(coin) or {}).get("long") or 0)
        ret = (px - entry) / entry
        if long_n < min_agree:
            _close_pos(
                data,
                coin,
                px,
                reason=f"elites_exited_{long_n}/{min_agree}",
                fee_rate=fee_rate,
            )
        elif ret <= -hard_stop:
            _close_pos(data, coin, px, reason="hard_stop", fee_rate=fee_rate)

    # OPEN: long_count >= min_agree and not already holding.
    opened = 0
    ranked = sorted(
        (
            (coin, sig)
            for coin, sig in signals.items()
            if int(sig.get("long") or 0) >= min_agree
            and coin not in (data.get("positions") or {})
        ),
        key=lambda x: -int(x[1].get("long") or 0),
    )
    for coin, sig in ranked:
        open_n = len(data.get("positions") or {})
        if open_n >= max_pos:
            break
        cash = float(data.get("cash") or 0)
        spend = cash * pos_pct
        if spend < min_spend:
            break
        px = _mid(mids, coin)
        if px <= 0:
            continue
        if _open_long(
            data,
            coin,
            px,
            spend,
            voters=list(sig.get("voters") or []),
            agree=int(sig.get("long") or 0),
            fee_rate=fee_rate,
        ):
            opened += 1

    _mark_positions(data, mids)
    data["last_tick_at"] = _now_iso()
    data["last_tick_reason"] = reason
    bs = book_status()
    data["book"] = bs
    data["last_tick_note"] = (
        f"elites={sum(1 for e in elites if e.get('ok'))} "
        f"long_sigs={sum(1 for s in signals.values() if s.get('side'))} "
        f"pos={len(data.get('positions') or {})} "
        f"book={bs.get('ws')}ws/{bs.get('rest')}rest "
        f"opened={opened} recon={reconciled} "
        f"fee={fee_rate * 100:.2f}% cash_pct={pos_pct * 100:.0f}"
    )
    data["tick_skipped"] = False

    with _lock:
        _write(data)
    guard.mark_used()
    if reason == "fill":
        _tick_guard.mark_used()
    logger.info("consensus tick (%s) %s", reason, data["last_tick_note"])
    return data


def on_desk_fill(
    address: str | None = None,
    fills: list[Any] | None = None,
    *,
    bot_id: str | None = None,
    is_snapshot: bool = False,
) -> dict[str, Any] | None:
    """WS path: update book from fills; tick only on live fills (not every snapshot)."""
    if not consensus_enabled():
        return None
    try:
        if is_snapshot:
            if address:
                seed_wallet_from_rest(address, bot_id, force=True)
            return None
        if address and fills is not None:
            n = apply_ws_fills(
                address, fills, bot_id=bot_id, is_snapshot=False
            )
            if n <= 0:
                return None
        return tick_consensus(force=False, reason="fill")
    except Exception as exc:  # noqa: BLE001
        logger.debug("consensus fill tick: %s", exc)
        return None


def status_snapshot() -> dict[str, Any]:
    data = load_consensus()
    return {
        "enabled": consensus_enabled(),
        "equity": data.get("equity"),
        "cash": data.get("cash"),
        "realized_pnl": data.get("realized_pnl"),
        "unrealized_pnl": data.get("unrealized_pnl"),
        "open_positions": len(data.get("positions") or {}),
        "warmed": data.get("warmed"),
        "last_tick_at": data.get("last_tick_at"),
        "last_tick_note": data.get("last_tick_note"),
        "last_tick_reason": data.get("last_tick_reason"),
        "stats": data.get("stats"),
        "config": data.get("config") or consensus_config(),
        "book": book_status(),
        "signals": data.get("signals") or {},
        "positions": data.get("positions") or {},
        "elites": [
            {
                "id": e.get("id"),
                "ok": e.get("ok"),
                "av": e.get("av"),
                "n_pos": len(e.get("positions") or []),
                "source": e.get("source"),
            }
            for e in (data.get("elites") or [])
        ],
    }
