"""Box-breakout scanner API (TradeGenuis engine · A-share + crypto)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from utils.box_breakout_runtime import (
    enabled,
    get_config,
    get_crypto,
    get_hot,
    get_kline,
    get_quotes,
    get_watchlist,
    pool_stocks,
    save_config,
    start_scan,
    status as runtime_status,
    update_pool,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/box", tags=["box-breakout"])


def _require_enabled() -> None:
    if not enabled():
        raise HTTPException(status_code=503, detail="box breakout disabled (NEXT_K_BOX_ENABLED=0)")


@router.get("/health")
async def box_health():
    st = await run_in_threadpool(runtime_status)
    return {"ok": True, **{k: st[k] for k in ("enabled", "scanning", "last_scan", "data_dir", "vendor_root")}}


@router.get("/status")
async def box_status():
    _require_enabled()
    return await run_in_threadpool(runtime_status)


@router.get("/watchlist")
async def box_watchlist():
    _require_enabled()
    return await run_in_threadpool(get_watchlist)


@router.get("/crypto")
async def box_crypto():
    _require_enabled()
    return await run_in_threadpool(get_crypto)


@router.get("/hot")
async def box_hot():
    _require_enabled()
    return await run_in_threadpool(get_hot)


@router.get("/config")
async def box_get_config():
    _require_enabled()
    return await run_in_threadpool(get_config)


@router.post("/config")
async def box_set_config(request: Request):
    _require_enabled()
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON object required")
    cfg = await run_in_threadpool(save_config, body)
    return {"ok": True, "config": cfg}


@router.get("/pool")
async def box_get_pool():
    _require_enabled()
    stocks = await run_in_threadpool(pool_stocks)
    return {"stocks": stocks}


@router.post("/pool")
async def box_pool(request: Request):
    _require_enabled()
    body = await request.json()
    action = str(body.get("action") or "")
    code = str(body.get("code") or "")
    name = str(body.get("name") or "")
    theme = str(body.get("theme") or "")
    out = await run_in_threadpool(update_pool, action, code, name, theme)
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error") or "bad request")
    return out


@router.get("/quotes")
async def box_quotes(codes: str = Query("", description="comma-separated A-share codes")):
    _require_enabled()
    code_list = [c for c in codes.split(",") if c.isdigit()][:100]
    return await run_in_threadpool(get_quotes, code_list)


@router.get("/kline")
async def box_kline(
    code: str = Query(...),
    market: str = Query("stock", description="stock | crypto"),
    lmt: int = Query(160, ge=60, le=500),
):
    _require_enabled()
    market = market if market in ("stock", "crypto") else "stock"
    if market == "stock" and not code.isdigit():
        raise HTTPException(status_code=400, detail="stock code must be digits")
    return await run_in_threadpool(get_kline, code, lmt, market)


@router.post("/scan")
async def box_scan(request: Request):
    _require_enabled()
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    mode = str(body.get("mode") or "crypto")
    top = body.get("top")
    top_i = int(top) if top is not None else None
    return await run_in_threadpool(start_scan, mode, top_i)
