# TradeGenuis · 箱体突破（Next K Protocol vendor）

Upstream: [Theclues/TradeGenuis-box](https://github.com/Theclues/TradeGenuis-box)

Integrated into Next K Protocol as `/api/box/*` (see `utils/box_breakout_runtime.py`, `routers/box.py`).
Frontend dashboard: `next-k-frontend/box.html`.

Do not run `server.py` in production embed mode — Protocol owns the HTTP surface.
Scan results land under `$DATA_DIR/box_breakout/` (or `./box_breakout/` locally).

Disable with `NEXT_K_BOX_ENABLED=0`.
