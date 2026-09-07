"""TradeGenuis box-breakout scanner runtime (A-share + Binance crypto)."""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_scanner = None
_lock = threading.Lock()
_scan_lock = threading.Lock()

STATE: dict[str, Any] = {
    "scanning": False,
    "scan_mode": None,
    "scan_log": [],
    "last_scan": None,
    "last_error": None,
    "kline_cache": {},
    "quote_cache": {},
    "config": {
        "auto": False,
        "auto_times": ["11:30", "15:00"],
        "tg_token": "",
        "tg_chat": "",
    },
}
CACHE_TTL = 60.0


def enabled() -> bool:
    raw = (os.getenv("NEXT_K_BOX_ENABLED", "1") or "1").strip().lower()
    return raw in ("1", "true", "yes", "on")


def ashare_enabled() -> bool:
    """A-share market/quick/pool scans. Default OFF; set NEXT_K_BOX_ASHARE_ENABLED=1 to open."""
    raw = (os.getenv("NEXT_K_BOX_ASHARE_ENABLED", "0") or "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def vendor_root() -> Path:
    raw = (os.getenv("TRADEGENIUS_BOX_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (PROJECT_ROOT / "vendor" / "tradegenius_box").resolve()


def resolve_data_dir() -> Path:
    raw = (os.getenv("DATA_DIR") or "").strip()
    if raw:
        base = Path(raw).expanduser()
    else:
        for candidate in (Path("/app/data"), Path("/data")):
            if candidate.is_dir():
                base = candidate
                break
        else:
            base = PROJECT_ROOT
    return base / "box_breakout"


def _load_scanner():
    global _scanner
    if _scanner is not None:
        return _scanner
    with _lock:
        if _scanner is not None:
            return _scanner
        root = vendor_root()
        path = root / "scanner.py"
        if not path.is_file():
            raise FileNotFoundError(f"tradegenius scanner missing: {path}")
        spec = importlib.util.spec_from_file_location("tradegenius_box_scanner", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load scanner from {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        data_dir = resolve_data_dir()
        mod.configure_data_dir(data_dir)
        _load_config_into_state(mod)
        _scanner = mod
        logger.info("box-breakout scanner ready · data=%s", data_dir)
        return _scanner


def _load_config_into_state(sc) -> None:
    try:
        if sc.CONFIG_FILE.exists():
            data = json.loads(sc.CONFIG_FILE.read_text(encoding="utf-8"))
            for k in STATE["config"]:
                if k in data:
                    STATE["config"][k] = data[k]
    except Exception:
        pass


def _log(msg: str) -> None:
    line = f"{_load_scanner().now_str()} {msg}"
    logger.info("box: %s", msg)
    with _lock:
        STATE["scan_log"].append(line)
        STATE["scan_log"] = STATE["scan_log"][-50:]


def status() -> dict[str, Any]:
    sc = _load_scanner()
    watch = read_json(sc.WATCH_FILE, {})
    crypto = read_json(sc.CRYPTO_FILE, {})
    with _lock:
        return {
            "ok": True,
            "enabled": enabled(),
            "ashare_enabled": ashare_enabled(),
            "scanning": STATE["scanning"],
            "scan_mode": STATE["scan_mode"],
            "last_scan": STATE["last_scan"],
            "last_error": STATE["last_error"],
            "scan_log": list(STATE["scan_log"][-16:]),
            "as_of": watch.get("as_of"),
            "crypto_as_of": crypto.get("as_of"),
            "is_trading_time": sc.is_trading_time(),
            "vendor_root": str(vendor_root()),
            "data_dir": str(resolve_data_dir()),
            "config": dict(STATE["config"]),
        }


def read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def get_watchlist() -> dict:
    sc = _load_scanner()
    return read_json(sc.WATCH_FILE, {"as_of": None, "candidates": [], "scope": "market"})


def get_crypto() -> dict:
    sc = _load_scanner()
    return read_json(sc.CRYPTO_FILE, {"as_of": None, "candidates": [], "scope": "crypto"})


def get_hot() -> dict:
    if not ashare_enabled():
        return {"hot_topics": [], "ashare_enabled": False}
    sc = _load_scanner()
    hot, _ = sc.fetch_hot_topics()
    return {"hot_topics": hot, "ashare_enabled": True}


def get_config() -> dict:
    _load_scanner()
    with _lock:
        return dict(STATE["config"])


def save_config(cfg: dict) -> dict:
    sc = _load_scanner()
    with _lock:
        for k in STATE["config"]:
            if k in cfg:
                STATE["config"][k] = cfg[k]
        data = dict(STATE["config"])
    sc.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    sc.CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _log("配置已保存")
    return data


def pool_stocks() -> list[dict]:
    sc = _load_scanner()
    return read_json(sc.POOL_FILE, {"stocks": []}).get("stocks", [])


def save_pool(stocks: list[dict]) -> None:
    sc = _load_scanner()
    sc.POOL_FILE.parent.mkdir(parents=True, exist_ok=True)
    sc.POOL_FILE.write_text(
        json.dumps({"updated": sc.now_str(), "stocks": stocks}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def update_pool(action: str, code: str, name: str = "", theme: str = "") -> dict:
    sc = _load_scanner()
    stocks = pool_stocks()
    code = str(code or "").strip()
    if action == "add" and code.isdigit():
        if not any(s.get("code") == code for s in stocks):
            if not name:
                try:
                    name = sc.fetch_quote(code).get("name", code)
                except Exception:
                    name = code
            stocks.append({"code": code, "name": name, "theme": theme or ""})
            save_pool(stocks)
        return {"ok": True, "stocks": stocks}
    if action == "remove":
        stocks = [s for s in stocks if s.get("code") != code]
        save_pool(stocks)
        return {"ok": True, "stocks": stocks}
    return {"ok": False, "error": "invalid action/code", "stocks": stocks}


def get_quotes(codes: list[str]) -> dict:
    if not ashare_enabled():
        return {}
    sc = _load_scanner()
    out: dict[str, Any] = {}
    need: list[str] = []
    now = time.time()
    for c in codes:
        hit = STATE["quote_cache"].get(c)
        if hit and now - hit[0] < 2.5:
            out[c] = hit[1]
        else:
            need.append(c)

    def one(c: str):
        try:
            return c, sc.fetch_quote(c)
        except Exception:
            return c, None

    if need:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for c, qq in ex.map(one, need):
                payload = None
                if qq:
                    payload = {
                        "price": qq["price"],
                        "chg": qq["chg"],
                        "turnover": qq["turnover"],
                        "volume_ratio": qq["volume_ratio"],
                    }
                STATE["quote_cache"][c] = (time.time(), payload)
                out[c] = payload
    return out


def get_kline(code: str, lmt: int = 160, market: str = "stock") -> dict:
    if market != "crypto" and not ashare_enabled():
        return {"code": code, "error": "A-share disabled (NEXT_K_BOX_ASHARE_ENABLED=0)"}
    sc = _load_scanner()
    key = ("k", market, code)
    with _lock:
        cached = STATE["kline_cache"].get(key)
        if cached and time.time() - cached[0] < CACHE_TTL:
            return cached[1]
    try:
        if market == "crypto":
            bars = sc.fetch_crypto_kline(code)
            box = sc.compute_box(bars)
            payload = {
                "code": code,
                "name": code,
                "price": bars[-1]["close"] if bars else None,
                "chg": None,
                "turnover": None,
                "volume_ratio": None,
                "bar_date": bars[-1]["date"] if bars else None,
                "bars": bars,
                "box": box,
            }
        else:
            quote = sc.fetch_quote(code)
            bars = sc.fetch_kline(code, lmt=lmt)
            box = sc.compute_box(bars)
            payload = {
                "code": code,
                "name": quote.get("name", ""),
                "price": quote["price"],
                "chg": quote["chg"],
                "turnover": quote["turnover"],
                "volume_ratio": quote["volume_ratio"],
                "bar_date": bars[-1]["date"] if bars else None,
                "bars": bars,
                "box": box,
            }
        with _lock:
            STATE["kline_cache"][key] = (time.time(), payload)
        return payload
    except Exception as e:
        return {"code": code, "error": str(e)[:200]}


def _scan_worker(mode: str, top: int | None = None) -> None:
    sc = _load_scanner()
    progress: Callable[[str], None] = lambda m: _log(m)
    try:
        if mode == "market":
            rows = sc.run_market_scan(full=True, progress=progress)
        elif mode == "quick":
            rows = sc.run_market_scan(full=False, top=top or sc.MARKET_TOP, progress=progress)
        elif mode == "crypto":
            rows = sc.run_crypto_scan(top=sc.CRYPTO_TOP_N, progress=progress)
        else:
            rows = sc.run_scan(network=True, progress=progress)
        qualified = sum(1 for r in rows if r.get("qualified"))
        _log(f"扫描完成：{len(rows)} 只，达标 {qualified} 只")
        with _lock:
            STATE["last_scan"] = sc.now_str()
    except Exception as e:
        logger.exception("box scan failed mode=%s", mode)
        with _lock:
            STATE["last_error"] = str(e)[:300]
        _log(f"扫描失败: {e}")
    finally:
        with _lock:
            STATE["scanning"] = False
            STATE["scan_mode"] = None


def start_scan(mode: str = "crypto", top: int | None = None) -> dict:
    if mode not in ("pool", "market", "quick", "crypto"):
        mode = "crypto"
    if mode in ("pool", "market", "quick") and not ashare_enabled():
        return {
            "status": "disabled",
            "msg": "A股扫描已关闭（NEXT_K_BOX_ASHARE_ENABLED=0）",
            "mode": mode,
        }
    with _scan_lock:
        if STATE["scanning"]:
            return {"status": "running", "msg": "扫描进行中", "mode": STATE.get("scan_mode")}
        STATE["scanning"] = True
        STATE["scan_mode"] = mode
        STATE["last_error"] = None
    labels = {
        "market": "（A股全市场全量）",
        "quick": "（A股快扫）",
        "crypto": "（币圈）",
        "pool": "（自选池）",
    }
    _log("手动触发扫描…" + labels.get(mode, ""))
    threading.Thread(target=_scan_worker_safe, args=(mode, top), daemon=True).start()
    return {"status": "started", "mode": mode}


def _scan_worker_safe(mode: str, top: int | None) -> None:
    try:
        _scan_worker(mode, top)
    except Exception:
        logger.exception("box scan thread crashed")
