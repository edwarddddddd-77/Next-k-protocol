# Polymarket 跟单台（Protocol）

盯某个公开代理钱包的新成交，纸面（默认）或 CLOB 实盘同方向跟 Yes/No。

## 开关

| 变量 | 默认 | 说明 |
|------|------|------|
| `POLY_DESK_ENABLED` | `0` | 挂载 `/api/poly-copy/*` 并启动运行时 |
| `POLY_COPY_ENABLED` | `0` | 启动跟单监督器（WS + Data API 补漏） |
| `POLY_LIVE` | `0` | `1` 时走 CLOB 实盘（需私钥与 API 凭证）；默认纸面 |
| `POLY_TARGET_WALLET` | — | 可覆盖 watchlist 里的 `address` |

## 策略（对齐原 HL 桌面）

- 只跟**新成交**（`copy_current=false`），启动时记 baseline，不补历史仓
- 缩仓：`our_size = leader_trade_size × (our_equity / leader_equity)`（纯比例，无单笔 min/max）
- 纸面初始资金：默认 `5000`（`paper_balance` / `POLY_PAPER_BALANCE`）
- 去重：优先 `transactionHash` + asset + side（不按时间窗丢单）
- 连发：同市场同方向在 `coalesce_sec`（默认 2s）内合并成一笔再跟
- 仓位键：`conditionId` + `outcome`（Yes/No）+ `asset`（token id）

## 数据路径

- 快路径：`wss://ws-live-data.polymarket.com` activity（本地按 `proxyWallet` 过滤）
- 补漏：Data API `GET /activity?user=…&type=TRADE`（约 3–5s）
- 对方权益：`GET /value?user=…`；持仓：`GET /positions?user=…`

## 路由

`/api/poly-copy/watchlist|paper|paper/sync-positions|copy/status|leader`

## 前端

`next-k-frontend/poly-copy.html`（导航 TAB「Poly台」），默认打 Protocol `resolveProtocolBase()`。

## 代码

- `routers/poly_copy.py`
- `utils/poly_*.py`
- `poly_watchlist.json`：优先仓库根 / `POLY_WATCHLIST_PATH`（避免 `DATA_DIR` 旧副本盖住部署）
- `poly_paper_copy.json`：纸面账本写入 `DATA_DIR`
