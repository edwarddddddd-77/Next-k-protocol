"""Polymarket paper copy — fill-follow only (copy_current=false).

Position model: market (conditionId) + Yes/No (outcome) + shares.
Sizing: our_delta = leader_fill_size × (our_equity / leader_equity).
Guards: baseline (no history), tx-hash dedupe, burst coalesce, min/max notional.

Burst handling (open-source style):
  • Deduplicate by transaction hash (+ asset + side), not by time window drops.
  • Coalesce same bot/market/side fills within coalesce_sec into one copy so
    dust clips in a sweep still sum past min_order_usd.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.poly_config import load_watchlist, resolve_data_dir
from utils.poly_data_api import normalize_trade

logger = logging.getLogger(__name__)

PAPER_NAME = "poly_paper_copy.json"
_lock = threading.Lock()
_leader_equity_mono: dict[str, float] = {}  # bot_id -> monotonic (not persisted)
_seen_cache: set[str] | None = None
# bot_id -> open coalesce slice
_slices: dict[str, dict[str, Any]] = {}

# Skip reasons that must not consume the dedupe slot (retry on later poll/WS).
_RETRIABLE_SKIP = frozenset({"no_baseline", "leader_equity_unknown"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paper_path() -> Path:
    return resolve_data_dir() / PAPER_NAME


def paper_config() -> dict[str, Any]:
    return {
        "copy_current": False,
        "live": _env_bool("POLY_LIVE", False),
        "poll_sec": float(os.getenv("POLY_POLL_SEC") or 4),
        "value_ttl_sec": float(os.getenv("POLY_VALUE_TTL_SEC") or 30),
        "coalesce_sec": float(
            os.getenv("POLY_COALESCE_SEC")
            if os.getenv("POLY_COALESCE_SEC") not in (None, "")
            else 2
        ),
    }


def _env_bool(key: str, default: bool) -> bool:
    raw = (os.getenv(key) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _empty_book() -> dict[str, Any]:
    return {
        "updated": _now_iso(),
        "venue": "polymarket",
        "mode": "paper",
        "bots": {},
        "seen_trades": [],
        "config": paper_config(),
    }


def _reset_seen_cache(book: dict[str, Any] | None = None) -> set[str]:
    global _seen_cache
    if book is None:
        _seen_cache = set()
    else:
        _seen_cache = set(book.get("seen_trades") or [])
    return _seen_cache


def _get_seen_cache(book: dict[str, Any]) -> set[str]:
    global _seen_cache
    if _seen_cache is None:
        return _reset_seen_cache(book)
    return _seen_cache


def load_paper() -> dict[str, Any]:
    path = _paper_path()
    bak = Path(str(path) + ".bak")
    if not path.is_file():
        if bak.is_file():
            try:
                shutil.copy2(bak, path)
            except Exception:
                pass
        else:
            book = _empty_book()
            _reset_seen_cache(book)
            return book
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("bots", {})
            data.setdefault("seen_trades", [])
            data.setdefault("config", paper_config())
            # Never persist process-local monotonic clocks.
            for bot in (data.get("bots") or {}).values():
                if isinstance(bot, dict):
                    bot.pop("_leader_equity_mono", None)
            _reset_seen_cache(data)
            return data
    except Exception as exc:
        logger.warning("poly paper load failed: %s", exc)
        if bak.is_file():
            try:
                data = json.loads(bak.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    _reset_seen_cache(data)
                    return data
            except Exception:
                pass
    book = _empty_book()
    _reset_seen_cache(book)
    return book


def save_paper(book: dict[str, Any]) -> None:
    path = _paper_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    book["updated"] = _now_iso()
    book["config"] = paper_config()
    # Strip non-JSON / process-local fields before write.
    for bot in (book.get("bots") or {}).values():
        if isinstance(bot, dict):
            bot.pop("_leader_equity_mono", None)
    tmp = path.with_suffix(".tmp")
    text = json.dumps(book, ensure_ascii=False, indent=2)
    tmp.write_text(text, encoding="utf-8")
    if path.is_file():
        try:
            shutil.copy2(path, Path(str(path) + ".bak"))
        except Exception:
            pass
    tmp.replace(path)


def _pos_key(trade: dict[str, Any]) -> str:
    asset = str(trade.get("asset") or "").strip()
    if asset:
        return f"asset:{asset}"
    cid = str(trade.get("condition_id") or "").strip()
    outcome = str(trade.get("outcome") or "").strip()
    return f"mkt:{cid}:{outcome}"


def _new_bot(bid: str, w: dict[str, Any]) -> dict[str, Any]:
    bal = float(w["paper_balance"])
    return {
        "id": bid,
        "address": w["address"],
        "paper_balance": bal,
        "balance": bal,
        "equity": bal,
        "realized_pnl": 0.0,
        "copy_ratio": None,
        "leader_equity": None,
        "leader_equity_at": None,
                    "max_order_usd": w["max_order_usd"],
                    "min_order_usd": w["min_order_usd"],
                    "coalesce_sec": float(
                        2 if w.get("coalesce_sec") is None else w.get("coalesce_sec")
                    ),
                    "debounce_sec": 0.0,
                    "copy_current": False,
        "baseline_ts": None,
        "baseline_set_at": None,
        "positions": {},
        "fills": [],
        "skips": [],
        "stats": {
            "fills_copied": 0,
            "fills_skipped": 0,
            "notional_copied": 0.0,
        },
    }


def ensure_bots_from_watchlist() -> dict[str, Any]:
    with _lock:
        book = load_paper()
        bots = book.setdefault("bots", {})
        wanted = {w["id"]: w for w in load_watchlist()}
        created: list[str] = []
        for bid, w in wanted.items():
            bot = bots.get(bid)
            if not isinstance(bot, dict):
                bots[bid] = _new_bot(bid, w)
                created.append(bid)
            else:
                bot["address"] = w["address"]
                bot["paper_balance"] = float(w["paper_balance"])
                bot["max_order_usd"] = float(w["max_order_usd"])
                bot["min_order_usd"] = float(w["min_order_usd"])
                bot["coalesce_sec"] = float(
                    2 if w.get("coalesce_sec") is None else w.get("coalesce_sec")
                )
                bot["debounce_sec"] = 0.0
                bot["copy_current"] = False
                bot.setdefault("positions", {})
                bot.setdefault("fills", [])
                bot.setdefault("skips", [])
                bot.setdefault("stats", {})
        for bid in list(bots.keys()):
            if bid not in wanted:
                bots.pop(bid, None)
                _leader_equity_mono.pop(bid, None)
                _slices.pop(bid, None)
        # New seats start flat: baseline = now (no mid-book catch-up).
        now_ts = int(time.time())
        for bid in created:
            bot = bots[bid]
            bot["baseline_ts"] = now_ts
            bot["baseline_set_at"] = _now_iso()
            logger.info(
                "poly baseline bot=%s ts=%s (new seat, copy_current=false)",
                bid,
                now_ts,
            )
        save_paper(book)
        return book


def set_baseline_if_needed(bot_id: str, latest_trade_ts: int | None = None) -> None:
    """Mark baseline so mid-book history is never copied (copy_current=false)."""
    with _lock:
        book = load_paper()
        bot = (book.get("bots") or {}).get(bot_id)
        if not isinstance(bot, dict):
            return
        if bot.get("baseline_ts") is not None:
            return
        # Baseline is "now". latest_trade_ts is ignored for the cutoff itself —
        # we only need to ensure we don't backfill; poll uses start=baseline.
        base = int(time.time())
        bot["baseline_ts"] = base
        bot["baseline_set_at"] = _now_iso()
        save_paper(book)
        logger.info(
            "poly baseline bot=%s ts=%s (copy_current=false, no history)",
            bot_id,
            base,
        )


def bot_baseline_ts(bot_id: str) -> int | None:
    with _lock:
        book = load_paper()
        bot = (book.get("bots") or {}).get(bot_id)
        if not isinstance(bot, dict):
            return None
        ts = bot.get("baseline_ts")
        if ts is None:
            return None
        try:
            return int(ts)
        except (TypeError, ValueError):
            return None


def refresh_leader_equity(bot_id: str, force: bool = False) -> float | None:
    from utils.poly_data_api import fetch_portfolio_value

    with _lock:
        book = load_paper()
        bot = (book.get("bots") or {}).get(bot_id)
        if not isinstance(bot, dict):
            return None
        ttl = float(paper_config()["value_ttl_sec"])
        last_mono = _leader_equity_mono.get(bot_id, 0.0)
        if not force and last_mono and (time.monotonic() - last_mono) < ttl:
            try:
                return float(bot.get("leader_equity") or 0)
            except (TypeError, ValueError):
                return None
        addr = str(bot.get("address") or "")
    if not addr:
        return None
    value = fetch_portfolio_value(addr)
    with _lock:
        book = load_paper()
        bot = (book.get("bots") or {}).get(bot_id)
        if not isinstance(bot, dict):
            return value
        bot["leader_equity"] = value
        bot["leader_equity_at"] = _now_iso()
        _leader_equity_mono[bot_id] = time.monotonic()
        our_eq = _bot_equity(bot)
        if value > 0:
            bot["copy_ratio"] = our_eq / value
        save_paper(book)
    return value


def _bot_equity(bot: dict[str, Any]) -> float:
    try:
        cash = float(bot.get("balance") or 0)
    except (TypeError, ValueError):
        cash = 0.0
    pos_val = 0.0
    for p in (bot.get("positions") or {}).values():
        if not isinstance(p, dict):
            continue
        try:
            pos_val += float(p.get("shares") or 0) * float(
                p.get("mark") or p.get("avg_price") or 0
            )
        except (TypeError, ValueError):
            continue
    eq = cash + pos_val
    bot["equity"] = eq
    return eq


def _mark_seen(book: dict[str, Any], key: str, keep: int = 5000) -> None:
    seen = book.setdefault("seen_trades", [])
    cache = _get_seen_cache(book)
    if key in cache:
        return
    seen.append(key)
    cache.add(key)
    if len(seen) > keep:
        dropped = seen[: len(seen) - keep]
        del seen[: len(seen) - keep]
        for d in dropped:
            cache.discard(d)


def _record_skip(bot: dict[str, Any], trade: dict[str, Any], reason: str) -> None:
    skips = bot.setdefault("skips", [])
    skips.append(
        {
            "at": _now_iso(),
            "reason": reason,
            "dedupe_key": trade.get("dedupe_key"),
            "side": trade.get("side"),
            "size": trade.get("size"),
            "price": trade.get("price"),
            "title": trade.get("title"),
            "outcome": trade.get("outcome"),
        }
    )
    if len(skips) > 200:
        del skips[:-200]
    stats = bot.setdefault("stats", {})
    stats["fills_skipped"] = int(stats.get("fills_skipped") or 0) + 1


def _slice_key(trade: dict[str, Any]) -> str:
    return f"{_pos_key(trade)}|{trade.get('side')}"


def _coalesce_sec(bot: dict[str, Any]) -> float:
    try:
        raw = bot.get("coalesce_sec")
        if raw is None:
            raw = paper_config()["coalesce_sec"]
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 2.0


def _new_slice(trade: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": _slice_key(trade),
        "opened_mono": time.monotonic(),
        "touched_mono": time.monotonic(),
        "trades": [trade],
        "dedupe_keys": [trade["dedupe_key"]],
    }


def _merge_slice_trade(sl: dict[str, Any]) -> dict[str, Any]:
    """Collapse buffered leader fills into one synthetic trade (size sum, VWAP)."""
    trades: list[dict[str, Any]] = list(sl.get("trades") or [])
    base = dict(trades[0])
    total_sz = 0.0
    notional = 0.0
    for t in trades:
        sz = float(t.get("size") or 0)
        px = float(t.get("price") or 0)
        total_sz += sz
        notional += sz * px
    px = (notional / total_sz) if total_sz > 0 else float(base.get("price") or 0)
    base["size"] = total_sz
    base["price"] = px
    base["usdc_size"] = notional
    base["source"] = "coalesce"
    base["coalesce_n"] = len(trades)
    base["dedupe_key"] = "coalesce|" + "|".join(sl.get("dedupe_keys") or [])
    base["transaction_hash"] = ",".join(
        str(t.get("transaction_hash") or "") for t in trades if t.get("transaction_hash")
    )[:200]
    return base


def _flush_slice(book: dict[str, Any], bot: dict[str, Any]) -> dict[str, Any] | None:
    bid = str(bot.get("id") or "")
    sl = _slices.pop(bid, None)
    if not sl or not sl.get("trades"):
        return None
    merged = _merge_slice_trade(sl)
    result = _apply_to_bot(bot, merged)
    # Consume child hashes after attempt (except retriable — put slice back).
    if result.get("reason") in _RETRIABLE_SKIP:
        _slices[bid] = sl
        return result
    for key in sl.get("dedupe_keys") or []:
        _mark_seen(book, key)
    return result


def flush_coalesce_slices(force: bool = False) -> list[dict[str, Any]]:
    """Flush expired (or all, if force) coalesce buffers. Called by supervisor."""
    out: list[dict[str, Any]] = []
    with _lock:
        book = load_paper()
        bots = book.get("bots") or {}
        now = time.monotonic()
        for bid in list(_slices.keys()):
            bot = bots.get(bid)
            if not isinstance(bot, dict):
                _slices.pop(bid, None)
                continue
            sl = _slices.get(bid)
            if not sl:
                continue
            window = _coalesce_sec(bot)
            age = now - float(sl.get("touched_mono") or sl.get("opened_mono") or now)
            if force or window <= 0 or age >= window:
                result = _flush_slice(book, bot)
                if result:
                    out.append(result)
        if out:
            save_paper(book)
    return out


def ingest_trade(raw: dict[str, Any], *, source: str = "unknown") -> dict[str, Any]:
    """Ingest one leader trade into matching paper bots. Returns summary."""
    trade = normalize_trade(raw, source=source)
    if not trade:
        return {"ok": False, "reason": "normalize_failed"}

    proxy = trade["proxy_wallet"]
    if not proxy:
        return {"ok": False, "reason": "no_proxy"}

    summary: dict[str, Any] = {
        "ok": True,
        "dedupe_key": trade["dedupe_key"],
        "applied": [],
        "skipped": [],
        "buffered": [],
    }

    with _lock:
        book = load_paper()
        cache = _get_seen_cache(book)
        if trade["dedupe_key"] in cache:
            summary["ok"] = False
            summary["reason"] = "duplicate"
            return summary
        # Already sitting in an open coalesce buffer?
        for sl in _slices.values():
            if trade["dedupe_key"] in (sl.get("dedupe_keys") or []):
                summary["ok"] = False
                summary["reason"] = "duplicate"
                return summary

        bots = book.get("bots") or {}
        matched = [
            b
            for b in bots.values()
            if isinstance(b, dict) and str(b.get("address") or "").lower() == proxy
        ]
        if not matched:
            summary["ok"] = False
            summary["reason"] = "no_bot"
            return summary

        for bot in matched:
            bid = str(bot.get("id") or "")
            window = _coalesce_sec(bot)
            sk = _slice_key(trade)
            sl = _slices.get(bid)

            # Different slice or expired → flush previous first.
            if sl is not None:
                age = time.monotonic() - float(
                    sl.get("touched_mono") or sl.get("opened_mono") or 0
                )
                if sl.get("key") != sk or window <= 0 or age >= window:
                    flushed = _flush_slice(book, bot)
                    if flushed:
                        if flushed.get("copied"):
                            summary["applied"].append(flushed)
                        else:
                            summary["skipped"].append(flushed)

            if window <= 0:
                result = _apply_to_bot(bot, trade)
                if result.get("reason") in _RETRIABLE_SKIP:
                    summary["skipped"].append(result)
                else:
                    _mark_seen(book, trade["dedupe_key"])
                    if result.get("copied"):
                        summary["applied"].append(result)
                    else:
                        summary["skipped"].append(result)
                continue

            # Buffer into coalesce slice (do not mark seen until flush).
            sl = _slices.get(bid)
            if sl is None or sl.get("key") != sk:
                _slices[bid] = _new_slice(trade)
            else:
                sl["trades"].append(trade)
                sl["dedupe_keys"].append(trade["dedupe_key"])
                sl["touched_mono"] = time.monotonic()
            summary["buffered"].append(
                {
                    "bot_id": bid,
                    "slice_key": sk,
                    "n": len((_slices.get(bid) or {}).get("trades") or []),
                }
            )

        save_paper(book)
    return summary


def _apply_to_bot(bot: dict[str, Any], trade: dict[str, Any]) -> dict[str, Any]:
    bid = str(bot.get("id") or "")
    baseline = bot.get("baseline_ts")
    ts = int(trade.get("timestamp") or 0)
    if baseline is None:
        _record_skip(bot, trade, "no_baseline")
        return {"bot_id": bid, "copied": False, "reason": "no_baseline"}
    if ts and ts <= int(baseline):
        _record_skip(bot, trade, "before_baseline")
        return {"bot_id": bid, "copied": False, "reason": "before_baseline"}

    leader_eq = float(bot.get("leader_equity") or 0)
    our_eq = _bot_equity(bot)
    if leader_eq <= 0:
        _record_skip(bot, trade, "leader_equity_unknown")
        return {"bot_id": bid, "copied": False, "reason": "leader_equity_unknown"}
    ratio = our_eq / leader_eq
    bot["copy_ratio"] = ratio

    price = float(trade["price"])
    if price <= 0:
        _record_skip(bot, trade, "bad_price")
        return {"bot_id": bid, "copied": False, "reason": "bad_price"}

    raw_shares = float(trade["size"]) * ratio
    notional = raw_shares * price
    min_usd = float(bot.get("min_order_usd") or 1)
    max_usd = float(bot.get("max_order_usd") or 50)

    if notional < min_usd:
        _record_skip(bot, trade, "dust_min_usd")
        return {
            "bot_id": bid,
            "copied": False,
            "reason": "dust_min_usd",
            "notional": notional,
        }

    if notional > max_usd:
        raw_shares = max_usd / price
        notional = max_usd

    side = trade["side"]
    key = _pos_key(trade)
    positions = bot.setdefault("positions", {})
    pos = positions.get(key)
    if not isinstance(pos, dict):
        pos = {
            "key": key,
            "asset": trade.get("asset"),
            "condition_id": trade.get("condition_id"),
            "outcome": trade.get("outcome"),
            "title": trade.get("title"),
            "slug": trade.get("slug"),
            "shares": 0.0,
            "avg_price": 0.0,
            "mark": price,
        }
        positions[key] = pos

    shares = float(pos.get("shares") or 0)
    avg = float(pos.get("avg_price") or 0)
    cash = float(bot.get("balance") or 0)
    realized = float(bot.get("realized_pnl") or 0)

    if side == "BUY":
        cost = raw_shares * price
        if cost > cash and price > 0:
            raw_shares = cash / price
            cost = raw_shares * price
            notional = cost
        if raw_shares <= 0 or cost < min_usd:
            _record_skip(bot, trade, "insufficient_cash")
            return {"bot_id": bid, "copied": False, "reason": "insufficient_cash"}
        new_shares = shares + raw_shares
        if new_shares > 0:
            pos["avg_price"] = ((shares * avg) + cost) / new_shares
        pos["shares"] = new_shares
        bot["balance"] = cash - cost
    else:  # SELL
        sell_shares = min(shares, raw_shares) if shares > 0 else 0.0
        if sell_shares <= 0:
            _record_skip(bot, trade, "no_position_to_sell")
            return {"bot_id": bid, "copied": False, "reason": "no_position_to_sell"}
        proceeds = sell_shares * price
        realized += (price - avg) * sell_shares
        pos["shares"] = shares - sell_shares
        bot["balance"] = cash + proceeds
        bot["realized_pnl"] = realized
        notional = proceeds
        raw_shares = sell_shares
        if pos["shares"] <= 1e-12:
            positions.pop(key, None)

    if key in positions:
        positions[key]["mark"] = price
        positions[key]["title"] = trade.get("title") or positions[key].get("title")
        positions[key]["outcome"] = trade.get("outcome") or positions[key].get(
            "outcome"
        )

    fill = {
        "at": _now_iso(),
        "source": trade.get("source"),
        "dedupe_key": trade.get("dedupe_key"),
        "transaction_hash": trade.get("transaction_hash"),
        "side": side,
        "shares": raw_shares,
        "price": price,
        "notional": notional,
        "ratio": ratio,
        "leader_size": trade.get("size"),
        "coalesce_n": trade.get("coalesce_n") or 1,
        "asset": trade.get("asset"),
        "condition_id": trade.get("condition_id"),
        "outcome": trade.get("outcome"),
        "title": trade.get("title"),
        "timestamp": ts,
    }
    fills = bot.setdefault("fills", [])
    fills.append(fill)
    if len(fills) > 500:
        del fills[:-500]

    stats = bot.setdefault("stats", {})
    stats["fills_copied"] = int(stats.get("fills_copied") or 0) + 1
    stats["notional_copied"] = float(stats.get("notional_copied") or 0) + float(
        notional
    )
    _bot_equity(bot)

    logger.info(
        "poly paper copy bot=%s %s shares=%.4g @ %.4g ratio=%.4g n=%s %s/%s",
        bid,
        side,
        raw_shares,
        price,
        ratio,
        fill.get("coalesce_n"),
        trade.get("outcome"),
        (trade.get("title") or "")[:48],
    )
    return {
        "bot_id": bid,
        "copied": True,
        "side": side,
        "shares": raw_shares,
        "price": price,
        "notional": notional,
        "ratio": ratio,
        "coalesce_n": fill.get("coalesce_n"),
    }


def slim_paper_for_api(book: dict[str, Any] | None = None) -> dict[str, Any]:
    with _lock:
        book = book or load_paper()
        bots_out = []
        for bot in (book.get("bots") or {}).values():
            if not isinstance(bot, dict):
                continue
            _bot_equity(bot)
            bots_out.append(
                {
                    "id": bot.get("id"),
                    "address": bot.get("address"),
                    "balance": bot.get("balance"),
                    "equity": bot.get("equity"),
                    "paper_balance": bot.get("paper_balance"),
                    "realized_pnl": bot.get("realized_pnl"),
                    "copy_ratio": bot.get("copy_ratio"),
                    "leader_equity": bot.get("leader_equity"),
                    "baseline_ts": bot.get("baseline_ts"),
                    "max_order_usd": bot.get("max_order_usd"),
                    "min_order_usd": bot.get("min_order_usd"),
                    "coalesce_sec": bot.get("coalesce_sec"),
                    "debounce_sec": 0,
                    "positions": list((bot.get("positions") or {}).values()),
                    "fills": (bot.get("fills") or [])[-50:],
                    "skips": (bot.get("skips") or [])[-30:],
                    "stats": bot.get("stats") or {},
                }
            )
        return {
            "ok": True,
            "updated": book.get("updated"),
            "venue": "polymarket",
            "mode": "paper" if not paper_config()["live"] else "live",
            "config": book.get("config") or paper_config(),
            "seen_trades": len(book.get("seen_trades") or []),
            "bots": bots_out,
        }


def reset_paper() -> dict[str, Any]:
    with _lock:
        _leader_equity_mono.clear()
        _slices.clear()
        book = _empty_book()
        _reset_seen_cache(book)
        save_paper(book)
    ensure_bots_from_watchlist()
    return slim_paper_for_api()


def reset_paper_bot(bot_id: str) -> dict[str, Any]:
    bid = str(bot_id or "").strip()
    with _lock:
        book = load_paper()
        bot = (book.get("bots") or {}).get(bid)
        if not isinstance(bot, dict):
            raise LookupError(f"bot not found: {bid}")
        bal = float(bot.get("paper_balance") or 1000)
        now_ts = int(time.time())
        bot.update(
            {
                "balance": bal,
                "equity": bal,
                "realized_pnl": 0.0,
                "positions": {},
                "fills": [],
                "skips": [],
                # Re-arm baseline so running supervisor can keep following new fills.
                "baseline_ts": now_ts,
                "baseline_set_at": _now_iso(),
                "stats": {
                    "fills_copied": 0,
                    "fills_skipped": 0,
                    "notional_copied": 0.0,
                },
            }
        )
        _leader_equity_mono.pop(bid, None)
        _slices.pop(bid, None)
        save_paper(book)
    return slim_paper_for_api()
