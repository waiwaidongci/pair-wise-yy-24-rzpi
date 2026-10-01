# 电台播出与版权窗口排程

一个不依赖第三方包、使用 SQLite 和标准库 HTTP 服务的电台排程项目。系统把“计划排期”和“实际播出”分开保存，支持地区授权、日期窗口、禁播时段、节目冷却、赞助商间隔、直播临时替换、实播对账与版权越界检查。

## 运行

需要 Python 3.11+。

```bash
python app.py
```

默认端口为 `8111`，页面地址是 <http://127.0.0.1:8111>。第一次启动会创建 `radio.db` 并写入三条演示排期。也可以设置端口和数据库位置：

```bash
PORT=9000 RADIO_DB=/tmp/radio.db python app.py
```

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖完整流程：排期、临时替换、播放日志、按日期对账；同时覆盖时间重叠、未授权地区和实播错节目等失败场景。

## 主要 API

- `GET /api/state`：节目、排期和最近对账异常
- `POST /api/programs`：创建节目并授权地区
- `POST /api/programs/{id}/regions`：追加地区授权
- `POST /api/schedule`：创建排期
- `POST /api/slots/{id}/replace`：替换计划节目并重新校验
- `POST /api/playout`：登记实播记录（自动冻结当前授权快照）
- `POST /api/reconcile`：按日期生成漏播、错播、时长偏差和超授权异常
- `POST /api/ingest/batches`：接收/续传县域台离线回传包
- `GET /api/ingest/batches?station=`：回传包列表
- `GET /api/ingest/batch?station=&package=`：单个回传包及每段结论
- `GET /api/license?date=`：按日期列出每条实播的授权/错播/时长结论
- `GET /api/auth-snapshots/{id}`：授权快照内容
- `POST /api/programs/deauthorize-region`：收回地区授权（用于验证历史不被追溯）

## 县域台离线回传规则

- **按（台站，包号）只收一次**：已入账的包重放直接返回原结论，实播不重复新增（`playout_logs` 上有部分唯一索引兜底）。
- **重试只续缺失段**：已校验通过的段不可变；坏段可随重试更正。缺段时包停在 `receiving`，列在 `missing_segments`。
- **校验失败保留整包待核**：任一段与 manifest 摘要不符，整包为 `pending_review`，不产生任何实播。
- **编排改动后待核立即作废**：替换排期时，触及该日期、尚未入账的包立即置为 `voided`，并按当前节目单立即重算对账；同包号重发即按新单重新计算（错播按当前单）。
- **越权按播出时刻快照判定**：每包携带台站缓存单对应的授权快照（`auth_snapshot.entries`），在收包时冻结；已播段始终用该快照判定地区/日期窗口，后来收紧窗口不追溯。未带快照时回网时刻冻结一次当前授权。
- **三端同一结论**：回传包视图（`exception_kinds`）、授权视图（`/api/license`）、对账列表（`/api/reconcile`）共用 `playout_conclusions`，重播不会被当成漏播。

准备排期时填写 `air_date`、`start_time`、`program_id`、`region`。页面会直接显示校验错误，不会保存失败的排期。
