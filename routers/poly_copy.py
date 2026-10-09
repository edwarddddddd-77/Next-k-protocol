"""Polymarket paper / copy desk API."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/poly-copy", tags=["poly-copy"])


@router.get("/watchlist")
async def get_watchlist():
    from utils.poly_config import load_watchlist, load_watchlist_doc

    def _run():
        doc = load_watchlist_doc()
        return {
            "ok": True,
            **doc,
            "active": load_watchlist(),
        }

    return await run_in_threadpool(_run)


@router.get("/paper")
async def get_paper():
    from utils.poly_paper_copy import ensure_bots_from_watchlist, slim_paper_for_api

    def _run():
        ensure_bots_from_watchlist()
        return slim_paper_for_api()

    return await run_in_threadpool(_run)


@router.post("/paper/reset")
async def reset_paper_ledger():
    from utils.poly_paper_copy import reset_paper

    return await run_in_threadpool(reset_paper)


@router.post("/paper/reset/{bot_id}")
async def reset_paper_bot_ledger(bot_id: str):
    from utils.poly_paper_copy import reset_paper_bot

    bid = str(bot_id or "").strip()
    if not bid:
        raise HTTPException(status_code=400, detail="bot_id required")
    try:
        return await run_in_threadpool(reset_paper_bot, bid)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/copy/status")
async def get_copy_status():
    from utils.poly_copy_supervisor import poly_copy_supervisor

    return {"ok": True, **poly_copy_supervisor.status}


@router.get("/leader")
async def get_leader(
    refresh: bool = Query(False, description="force refresh portfolio value"),
):
    """Leader wallet value + open positions (public Data API)."""
    from utils.poly_config import load_watchlist
    from utils.poly_data_api import fetch_positions, fetch_portfolio_value
    from utils.poly_paper_copy import refresh_leader_equity

    def _run():
        wallets = load_watchlist()
        if not wallets:
            raise ValueError("no target wallet configured")
        out = []
        for w in wallets:
            if refresh:
                refresh_leader_equity(w["id"], force=True)
            value = fetch_portfolio_value(w["address"])
            positions = fetch_positions(w["address"], limit=100)
            out.append(
                {
                    "id": w["id"],
                    "address": w["address"],
                    "value": value,
                    "positions": [
                        {
                            "asset": p.get("asset"),
                            "condition_id": p.get("conditionId"),
                            "outcome": p.get("outcome"),
                            "size": p.get("size"),
                            "avg_price": p.get("avgPrice"),
                            "cur_price": p.get("curPrice"),
                            "current_value": p.get("currentValue"),
                            "title": p.get("title"),
                            "slug": p.get("slug"),
                        }
                        for p in positions
                    ],
                }
            )
        return {"ok": True, "leaders": out}

    try:
        return await run_in_threadpool(_run)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("poly leader fetch failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
