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
    watch_path = tmp_path / "poly_watchlist.json"
    monkeypatch.setenv("POLY_WATCHLIST_PATH", str(watch_path))
    monkeypatch.setenv("POLY_TARGET_WALLET", "0x" + "ab" * 20)
    watch = {
        "venue": "polymarket",
        "wallets": [
            {
                "id": "bot_poly",
                "address": "0x" + "ab" * 20,
                "paper_balance": 1000,
                "coalesce_sec": 0,
                "copy_current": False,
            }
        ],
    }
    watch_path.write_text(json.dumps(watch), encoding="utf-8")
    ppc._slices.clear()
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


def test_ratio_uncapped(paper_env):
    """Pure equity ratio — no min/max order USD clamp."""
    ppc.set_baseline_if_needed("bot_poly", int(time.time()) - 10)
    book = ppc.load_paper()
    bot = book["bots"]["bot_poly"]
    bot["leader_equity"] = 1000.0  # ratio 1.0
    bot["baseline_ts"] = int(time.time()) - 10
    ppc.save_paper(book)

    # 100 shares @ $0.5 → $50 ours at ratio 1.0 (full size, not capped)
    raw = _trade(size=100, price=0.5, transactionHash="0x" + "44" * 32)
    out = ppc.ingest_trade(raw, source="test")
    assert out.get("applied"), out
    book = ppc.load_paper()
    pos = list(book["bots"]["bot_poly"]["positions"].values())[0]
    assert pos["shares"] == pytest.approx(100.0)


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


def test_paper_balance_reseed_when_flat(paper_env, tmp_path: Path):
    book = ppc.load_paper()
    bot = book["bots"]["bot_poly"]
    assert bot["balance"] == pytest.approx(1000.0)
    bot["paper_balance"] = 1000.0
    bot["balance"] = 1000.0
    bot["positions"] = {}
    bot["fills"] = []
    ppc.save_paper(book)

    watch = {
        "venue": "polymarket",
        "wallets": [
            {
                "id": "bot_poly",
                "address": "0x" + "ab" * 20,
                "paper_balance": 5000,
                "coalesce_sec": 0,
                "copy_current": False,
            }
        ],
    }
    (tmp_path / "poly_watchlist.json").write_text(
        json.dumps(watch), encoding="utf-8"
    )
    ppc.ensure_bots_from_watchlist()
    book = ppc.load_paper()
    bot = book["bots"]["bot_poly"]
    assert bot["paper_balance"] == pytest.approx(5000.0)
    assert bot["balance"] == pytest.approx(5000.0)
    assert bot["equity"] == pytest.approx(5000.0)


def test_burst_no_longer_dropped_by_debounce(paper_env):
    """coalesce_sec=0 → each distinct tx is copied (open-source hash-dedupe style)."""
    now = int(time.time())
    book = ppc.load_paper()
    bot = book["bots"]["bot_poly"]
    bot["leader_equity"] = 10_000.0
    bot["baseline_ts"] = now - 10
    bot["coalesce_sec"] = 0
    ppc.save_paper(book)

    for i in range(3):
        raw = _trade(
            size=100,
            price=0.5,
            transactionHash="0x" + f"{i:02x}" * 32,
            timestamp=now + i,
        )
        out = ppc.ingest_trade(raw, source="test")
        assert out.get("applied"), out

    book = ppc.load_paper()
    pos = list(book["bots"]["bot_poly"]["positions"].values())[0]
    # 3 × (100 * 0.1) = 30 shares
    assert pos["shares"] == pytest.approx(30.0)
    assert book["bots"]["bot_poly"]["stats"]["fills_copied"] == 3


def test_coalesce_merges_burst(paper_env, monkeypatch):
    now = int(time.time())
    book = ppc.load_paper()
    bot = book["bots"]["bot_poly"]
    bot["leader_equity"] = 10_000.0
    bot["baseline_ts"] = now - 10
    bot["coalesce_sec"] = 2
    ppc.save_paper(book)

    # Three clips merge: 30+40+50 = 120 leader → 12 our shares at ratio 0.1.
    for i, sz in enumerate((30, 40, 50)):
        raw = _trade(
            size=sz,
            price=0.5,
            transactionHash="0x" + f"{10 + i:02x}" * 32,
            timestamp=now + 1,
        )
        out = ppc.ingest_trade(raw, source="test")
        assert out.get("buffered"), out

    flushed = ppc.flush_coalesce_slices(force=True)
    assert any(r.get("copied") for r in flushed), flushed
    book = ppc.load_paper()
    pos = list(book["bots"]["bot_poly"]["positions"].values())[0]
    assert pos["shares"] == pytest.approx(12.0)
    fill = book["bots"]["bot_poly"]["fills"][-1]
    assert fill.get("coalesce_n") == 3
