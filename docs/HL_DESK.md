# HL 映仓台（Protocol）

从 `next-k-api` 移植的 Hyperliquid → 纸面/Bitget/Binance 跟单桌面。`next-k-api` 保持不变；前端「映仓台」仍打 API，「映仓台·P」打本服务。

Protocol 桌面席位精简为 **C + F**（见 `hl_short_term_watchlist.json`）。实盘通常只开 C：`HL_BITGET_ENABLE_BOTS=bot_c`（主账户 `BITGET_*`）；F 默认纸面。

已移除：反马丁袖（AM）、跟单候选池（周筛 / `/candidates` / `hl_desk_candidates*`）。

跟单核心（`hl_copy_supervisor` / `hl_ws` / `hl_short_term` / Bitget·Binance executor 等）与 `next-k-api` **保持一致**；`hl_paper_copy.py` 在 api 同款逻辑上**剥离 AM**（勿整文件从 api 覆盖回来）。守卫单测：`tests/test_hl_desk_no_am_candidates.py`。

## 开关

| 变量 | 默认 | 说明 |
|------|------|------|
| `HL_DESK_ENABLED` | `1` | 挂载 `/api/hl-short/*` 并启动运行时 |
| `HL_COPY_ENABLED` | `1`（见 supervisor） | WS 跟单监督器 |
| `HL_BITGET_LIVE` / `HL_BINANCE_LIVE` | 通常 `0` | 实盘执行（与 API 侧相同语义） |

**注意：** 若 Railway 上 next-k-api 与 Protocol **同时** `HL_COPY_ENABLED=1` 且开了同一实盘，会对同一领导者双开跟单。切流时应只留一侧开启。

## 路由

`/api/hl-short/watchlist|board|paper|copy/status|live/*|f-mr`

网格代理开启时，`/api/hl-short` 已加入 Protocol keep 列表，不会被转到 wangge。

## 代码位置

- `routers/hl_short.py`
- `utils/hl_*.py`、`utils/avax_f_mr_indicator.py`、`utils/rate_limit.py`
- `utils/hl_desk_runtime.py`（启停）
- `quant/engine/exchanges/{bitget,binance}/`（REST；无 vnpy）
- 仓库根：`hl_short_term_watchlist.json` 等部署配置
