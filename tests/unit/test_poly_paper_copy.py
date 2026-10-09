"""Polymarket paper copy: baseline, dedupe, sizing, sell without position."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from utils import poly_paper_copy as ppc
from utils.poly_data_api import normalize_trade


@pytest.fixture()
def paper_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLY_TARGET_WALLET", "0x" + "ab" * 20)
    watch = {
        "venue": "polymarket",
        "wallets": [
            {
                "id": "bot_poly",
                "address": "0x" + "ab" * 20,
                "paper_balance": 1000,
                "max_order_usd": 100,
                "min_order_usd": 1,
                "debounce_sec": 0,
                "copy_current": False,
            }
        ],
    }
    (tmp_path / "poly_watchlist.json").write_text(
        json.dumps(watch), encoding="utf-8"
    )
    ppc._last_fill_mono.clear()
    ppc.ensure_bots_from_watchlist()
    return tmp_path


def _trade(**kwargs):
    base = {
        "proxyWallet": "0x" + "ab" * 20,
        "side": "BUY",
        "size": 100,
        "price": 0.5,
        "asset": "token-yes-1",
        "conditionId": "0x" + "cd" * 32,
        "outcome": "Yes",
        "title": "Test market",
        "transactionHash": "0x" + "11" * 32,
        "timestamp": int(time.time()) + 60,
        "usdcSize": 50,
    }
    base.update(kwargs)
    return base


def test_normalize_trade_dedupe_key():
    t = normalize_trade(_trade())
    assert t is not None
    assert t["side"] == "BUY"
    assert "token-yes-1" in t["dedupe_key"]


def test_baseline_skips_history(paper_env):
    now = int(time.time())
    book = ppc.load_paper()
    bot = book["bots"]["bot_poly"]
    bot["leader_equity"] = 10_000.0
    bot["baseline_ts"] = now
    ppc.save_paper(book)

    old = _trade(timestamp=now - 3600, transactionHash="0x" + "22" * 32)
    out = ppc.ingest_trade(old, source="test")
    assert out.get("skipped")
    assert out["skipped"][0]["reason"] == "before_baseline"


def test_copy_buy_scaled_and_dedupe(paper_env):
    ppc.set_baseline_if_needed("bot_poly", int(time.time()) - 10)
    book = ppc.load_paper()
    bot = book["bots"]["bot_poly"]
    bot["leader_equity"] = 10_000.0  # ratio = 1000/10000 = 0.1
    bot["baseline_ts"] = int(time.time()) - 10
    ppc.save_paper(book)

    raw = _trade(size=100, price=0.4)  # leader 100 shares → our 10
    out = ppc.ingest_trade(raw, source="test")
    assert out.get("applied"), out
    book = ppc.load_paper()
    bot = book["bots"]["bot_poly"]
    positions = list(bot["positions"].values())
    assert len(positions) == 1
    assert positions[0]["shares"] == pytest.approx(10.0)
    assert positions[0]["outcome"] == "Yes"

    # Same trade again → duplicate
    out2 = ppc.ingest_trade(raw, source="test")
    assert out2.get("reason") == "duplicate"


def test_sell_without_position_skipped(paper_env):
    ppc.set_baseline_if_needed("bot_poly", int(time.time()) - 10)
    book = ppc.load_paper()
    bot = book["bots"]["bot_poly"]
    bot["leader_equity"] = 10_000.0
    bot["baseline_ts"] = int(time.time()) - 10
    ppc.save_paper(book)

    raw = _trade(side="SELL", size=50, transactionHash="0x" + "33" * 32)
    out = ppc.ingest_trade(raw, source="test")
    assert out.get("skipped")
    assert out["skipped"][0]["reason"] == "no_position_to_sell"


def test_max_order_usd_cap(paper_env):
    ppc.set_baseline_if_needed("bot_poly", int(time.time()) - 10)
    book = ppc.load_paper()
    bot = book["bots"]["bot_poly"]
    bot["leader_equity"] = 1000.0  # ratio 1.0
    bot["max_order_usd"] = 20.0
    bot["baseline_ts"] = int(time.time()) - 10
    ppc.save_paper(book)

    # 100 shares * $0.5 = $50 leader → would be $50 ours, capped to $20 → 40 shares
    raw = _trade(size=100, price=0.5, transactionHash="0x" + "44" * 32)
    out = ppc.ingest_trade(raw, source="test")
    assert out.get("applied"), out
    book = ppc.load_paper()
    pos = list(book["bots"]["bot_poly"]["positions"].values())[0]
    assert pos["shares"] == pytest.approx(40.0)


def test_leader_equity_unknown_is_retriable(paper_env):
    now = int(time.time())
    book = ppc.load_paper()
    bot = book["bots"]["bot_poly"]
    bot["baseline_ts"] = now - 10
    bot["leader_equity"] = 0
    ppc.save_paper(book)

    raw = _trade(transactionHash="0x" + "55" * 32, timestamp=now + 5)
    out = ppc.ingest_trade(raw, source="test")
    assert out["skipped"][0]["reason"] == "leader_equity_unknown"
    # Must NOT consume dedupe — same trade retries after equity refresh.
    book = ppc.load_paper()
    bot = book["bots"]["bot_poly"]
    bot["leader_equity"] = 10_000.0
    ppc.save_paper(book)
    out2 = ppc.ingest_trade(raw, source="test")
    assert out2.get("applied"), out2


def test_normalize_rejects_zero_price():
    assert normalize_trade(_trade(price=0)) is None


def test_new_bot_gets_baseline(paper_env):
    book = ppc.load_paper()
    assert book["bots"]["bot_poly"].get("baseline_ts") is not None
