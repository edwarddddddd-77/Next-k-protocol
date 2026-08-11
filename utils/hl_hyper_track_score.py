"""hyper-track-style wallet scoring (ported from matthewrahm/hyper-track scorer).

Round-trips: position 0 → open → flat/flip.
Composite: 90d-window weights renormalized without longevity (×100/0.90).
Style: scalper / day_trader / swing_trader / position_trader + tags.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


@dataclass
class WindowMetrics:
    pnl: float = 0.0
    roi: float = 0.0
    win_rate: float = 0.0
    trades: int = 0
    sharpe: float = 0.0
    sortino: float = 0.0
    max_drawdown: float = 0.0
    profit_factor: float = 0.0
    reward_risk: float = 0.0
    avg_hold_hours: float = 0.0
    trades_per_day: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Trade:
    coin: str
    side: str
    entry_price: float
    exit_price: float
    size: float
    pnl: float
    fees: float
    net_pnl: float
    open_time: int
    close_time: int
    hold_time_hours: float


def _signed_size(fill: dict[str, Any]) -> float:
    # Prefer shared HL inventory sign (matches flat WR / paper)
    try:
        from utils.hl_wr_screen import _signed_fill_sz

        return float(_signed_fill_sz(fill) or 0.0)
    except Exception:
        pass
    try:
        sz = abs(float(fill.get("sz") or 0))
    except (TypeError, ValueError):
        return 0.0
    if sz <= 0:
        return 0.0
    side = str(fill.get("side") or "").strip().upper()
    if side in ("B", "BUY"):
        return sz
    if side in ("A", "SELL"):
        return -sz
    return 0.0


def group_into_trades(fills: list[dict[str, Any]]) -> list[Trade]:
    """Group HL fills into hyper-track round-trips."""
    items = sorted(
        (f for f in fills if isinstance(f, dict)),
        key=lambda f: int(f.get("time") or 0),
    )
    positions: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "net_size": 0.0,
            "fills": [],
            "entry_prices": [],
            "entry_sizes": [],
            "open_time": 0,
        }
    )
    trades: list[Trade] = []
    eps = 1e-12

    for fill in items:
        coin = str(fill.get("coin") or "").strip() or "?"
        signed = _signed_size(fill)
        if abs(signed) <= eps:
            continue
        try:
            px = float(fill.get("px") or 0)
        except (TypeError, ValueError):
            px = 0.0
        try:
            cp = float(fill.get("closedPnl") or 0)
        except (TypeError, ValueError):
            cp = 0.0
        try:
            fee = float(fill.get("fee") or 0)
        except (TypeError, ValueError):
            fee = 0.0
        try:
            ts = int(fill.get("time") or 0)
        except (TypeError, ValueError):
            continue

        pos = positions[coin]
        prev = float(pos["net_size"])
        pos["net_size"] = prev + signed
        pos["fills"].append({"closed_pnl": cp, "fee": fee})
        new = float(pos["net_size"])
        size_abs = abs(signed)

        if abs(prev) <= eps and abs(new) > eps:
            pos["entry_prices"] = [px]
            pos["entry_sizes"] = [size_abs]
            pos["open_time"] = ts
            pos["fills"] = [{"closed_pnl": cp, "fee": fee}]
        elif (
            abs(prev) > eps
            and (prev > 0) == (new > 0)
            and abs(new) > abs(prev) + eps
        ):
            pos["entry_prices"].append(px)
            pos["entry_sizes"].append(size_abs)

        crossed = (prev > eps and new <= eps) or (prev < -eps and new >= -eps)
        flipped = abs(prev) > eps and abs(new) > eps and (prev > 0) != (new > 0)
        if crossed or flipped:
            entry_sizes = list(pos["entry_sizes"] or [])
            entry_prices = list(pos["entry_prices"] or [])
            total_entry = sum(entry_sizes)
            if total_entry > 0:
                avg_entry = sum(p * s for p, s in zip(entry_prices, entry_sizes)) / total_entry
            else:
                avg_entry = px
            trade_pnl = sum(float(x["closed_pnl"]) for x in pos["fills"])
            trade_fees = sum(float(x["fee"]) for x in pos["fills"])
            open_time = int(pos.get("open_time") or ts)
            hold_h = max((ts - open_time) / 3_600_000, 0.0)
            trades.append(
                Trade(
                    coin=coin,
                    side="LONG" if prev > 0 else "SHORT",
                    entry_price=avg_entry,
                    exit_price=px,
                    size=abs(prev),
                    pnl=trade_pnl,
                    fees=trade_fees,
                    net_pnl=trade_pnl - trade_fees,
                    open_time=open_time,
                    close_time=ts,
                    hold_time_hours=hold_h,
                )
            )
            pos["fills"] = []
            pos["entry_prices"] = []
            pos["entry_sizes"] = []
            if abs(new) > eps:
                pos["entry_prices"] = [px]
                pos["entry_sizes"] = [abs(new)]
                pos["open_time"] = ts
                pos["fills"] = []
            else:
                pos["net_size"] = 0.0
                pos["open_time"] = 0

    return trades


def _avg_capital(trades: list[Trade]) -> float:
    if not trades:
        return 0.0
    events: list[tuple[int, float]] = []
    for t in trades:
        notional = t.size * t.entry_price
        events.append((t.open_time, notional))
        events.append((t.close_time, -notional))
    events.sort(key=lambda e: e[0])
    total_weighted = 0.0
    current = 0.0
    prev_t = events[0][0]
    for ts, delta in events:
        dur = ts - prev_t
        if dur > 0:
            total_weighted += current * dur
        current += delta
        prev_t = ts
    span = events[-1][0] - events[0][0]
    return total_weighted / span if span > 0 else abs(current)


def _drawdown(trades: list[Trade]) -> float:
    if not trades:
        return 0.0
    cum = peak = max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.close_time):
        cum += t.net_pnl
        if cum > peak:
            peak = cum
        dd = (peak - cum) / max(peak, 1.0) if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
    return max_dd


def _daily_returns(trades: list[Trade]) -> list[float]:
    daily: dict[int, float] = defaultdict(float)
    for t in trades:
        daily[t.close_time // 86_400_000] += t.net_pnl
    return list(daily.values())


def _sharpe(trades: list[Trade]) -> float:
    daily = _daily_returns(trades)
    if len(daily) < 2:
        return 0.0
    avg = sum(daily) / len(daily)
    var = sum((r - avg) ** 2 for r in daily) / len(daily)
    std = math.sqrt(var) if var > 0 else 0.0
    return (avg / std) * math.sqrt(365) if std > 0 else 0.0


def _sortino(trades: list[Trade]) -> float:
    daily = _daily_returns(trades)
    if len(daily) < 2:
        return 0.0
    avg = sum(daily) / len(daily)
    downside = [r for r in daily if r < 0]
    if not downside:
        return 0.0
    ddev = math.sqrt(sum(r * r for r in downside) / len(downside))
    return (avg / ddev) * math.sqrt(365) if ddev > 0 else 0.0


def compute_window(trades: list[Trade], now_ms: int, days: int | None) -> WindowMetrics:
    if days is not None:
        cutoff = now_ms - days * 86_400_000
        window = [t for t in trades if t.close_time >= cutoff]
    else:
        window = list(trades)
    if not window:
        return WindowMetrics()

    total_pnl = sum(t.net_pnl for t in window)
    winning = [t for t in window if t.net_pnl > 0]
    losing = [t for t in window if t.net_pnl < 0]
    wr = len(winning) / len(window)
    avg_cap = _avg_capital(window)
    roi = total_pnl / avg_cap if avg_cap > 0 else 0.0
    avg_win = sum(t.net_pnl for t in winning) / len(winning) if winning else 0.0
    avg_loss = sum(abs(t.net_pnl) for t in losing) / len(losing) if losing else 0.0
    rr = avg_win / avg_loss if avg_loss > 0 else 0.0
    gp = sum(t.net_pnl for t in winning)
    gl = abs(sum(t.net_pnl for t in losing))
    pf = gp / gl if gl > 0 else 0.0
    holds = [t.hold_time_hours for t in window if t.hold_time_hours > 0]
    avg_hold = sum(holds) / len(holds) if holds else 0.0
    first = min(t.open_time for t in window)
    last = max(t.close_time for t in window)
    span_days = max((last - first) / 86_400_000, 1.0)
    return WindowMetrics(
        pnl=total_pnl,
        roi=roi,
        win_rate=wr,
        trades=len(window),
        sharpe=_sharpe(window),
        sortino=_sortino(window),
        max_drawdown=_drawdown(window),
        profit_factor=pf,
        reward_risk=rr,
        avg_hold_hours=avg_hold,
        trades_per_day=len(window) / span_days,
    )


def classify_style(metrics: WindowMetrics, trades: list[Trade]) -> tuple[str, list[str]]:
    avg_hold = metrics.avg_hold_hours
    freq = metrics.trades_per_day
    if avg_hold < 1 and freq > 10:
        style = "scalper"
    elif avg_hold < 24 and freq > 2:
        style = "day_trader"
    elif avg_hold < 168:
        style = "swing_trader"
    else:
        style = "position_trader"

    tags: list[str] = []
    if avg_hold < 0.5 and freq > 20:
        tags.append("degen")
    if metrics.win_rate > 0.7 and avg_hold < 2:
        tags.append("sniper")
    if freq > 20 and 0.45 <= metrics.win_rate <= 0.55:
        tags.append("grinder")
    if trades:
        vols: dict[str, float] = defaultdict(float)
        for t in trades:
            vols[t.coin] += t.size * t.entry_price
        tot = sum(vols.values()) or 1.0
        mx = max(vols.values()) / tot
        if mx > 0.8:
            tags.append("concentrated")
        elif len(vols) >= 10 and mx < 0.3:
            tags.append("diversified")
    return style, tags


def composite_score(metrics: WindowMetrics) -> float:
    """0–100 composite (longevity weight folded out → /0.90), matching HT."""
    if metrics.trades <= 0:
        return 0.0
    roi_n = clamp(metrics.roi, -1, 5) / 5
    wr_n = metrics.win_rate
    sharpe_n = clamp(metrics.sharpe, -2, 5) / 5
    sortino_n = clamp(metrics.sortino, -2, 8) / 8
    dd_n = 1 - clamp(metrics.max_drawdown, 0, 1)
    pf_n = clamp(metrics.profit_factor, 0, 5) / 5
    rr_n = clamp(metrics.reward_risk, 0, 5) / 5
    vol_n = clamp(metrics.trades / 200, 0, 1)
    composite = (
        roi_n * 0.15
        + wr_n * 0.10
        + sharpe_n * 0.15
        + sortino_n * 0.10
        + dd_n * 0.15
        + pf_n * 0.10
        + rr_n * 0.05
        + vol_n * 0.10
    )
    return round(composite / 0.90 * 100, 1) if composite > 0 else 0.0


def score_fills(
    fills: list[dict[str, Any]],
    *,
    now_ms: int | None = None,
    primary_days: int = 30,
) -> dict[str, Any]:
    """Score a fill list. primary_days drives composite + style (HT uses 90)."""
    import time as _time

    now = int(now_ms if now_ms is not None else _time.time() * 1000)
    trades = group_into_trades(fills)
    primary = compute_window(trades, now, primary_days)
    w7 = compute_window(trades, now, 7)
    wall = compute_window(trades, now, None)
    style, tags = classify_style(primary, trades)
    coins = sorted({t.coin for t in trades})
    return {
        "composite": composite_score(primary),
        "style": style,
        "style_tags": tags,
        "primary_days": primary_days,
        "primary": primary.to_dict(),
        "w7": w7.to_dict(),
        "wall": wall.to_dict(),
        "n_trades_all": len(trades),
        "coins": ",".join(coins[:12]),
        "unique_coins": len(coins),
    }
