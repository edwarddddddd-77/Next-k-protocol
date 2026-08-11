"""Guard: Protocol HL desk matches api copy core, without AM sleeve or candidate pool."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UTILS = ROOT / "utils"
ROUTER = ROOT / "routers" / "hl_short.py"
PAPER = UTILS / "hl_paper_copy.py"


def test_candidate_pool_modules_absent() -> None:
    for name in (
        "hl_desk_candidates.py",
        "hl_wr_screen.py",
        "hl_hyper_track_score.py",
        "hl_desk_candidates.json",
    ):
        assert not (UTILS / name).exists(), f"candidate leftover: {name}"
        assert not (ROOT / name).exists(), f"candidate leftover: {name}"


def test_router_has_no_candidates_endpoint() -> None:
    text = ROUTER.read_text(encoding="utf-8")
    assert "/candidates" not in text
    assert "hl_desk_candidates" not in text
    assert "get_desk_candidates" not in text


def test_paper_copy_has_no_am_runtime() -> None:
    text = PAPER.read_text(encoding="utf-8")
    for banned in (
        "def _is_am_bot",
        "def _am_scale_mult",
        "def _am_settle_day",
        "def _am_maybe_mdd_break",
        "def _am_bots",
        "AM_SCALE_CAP",
        "AM_SCALE_STEP",
        "am_balance",
        "am_bot_count",
    ):
        assert banned not in text, f"AM runtime leftover: {banned}"
    # Defensive: watchlist may mention anti_martingale only to strip it.
    assert "Protocol: anti-martingale sleeve removed" in text


def test_core_copy_modules_match_api_when_present() -> None:
    """If sibling next-k-api exists, shared non-AM engines must stay byte-identical."""
    api_root = ROOT.parent / "next-k-api"
    if not api_root.is_dir():
        return
    shared = (
        "hl_copy_supervisor.py",
        "hl_ws.py",
        "hl_short_term.py",
        "hl_bitget_executor.py",
        "hl_bitget_subaccounts.py",
        "hl_bitget_symbol_map.py",
        "hl_binance_executor.py",
        "hl_binance_subaccounts.py",
        "hl_binance_symbol_map.py",
    )
    for name in shared:
        proto = (UTILS / name).read_bytes()
        api = (api_root / "utils" / name).read_bytes()
        assert proto == api, f"{name} diverged from next-k-api (copy logic must match)"
