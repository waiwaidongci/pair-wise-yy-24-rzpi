# 电台播出与版权窗口排程

一个不依赖第三方包、使用 SQLite 和标准库 HTTP 服务的电台排程项目。系统把“计划排期”和“实际播出”分开保存，支持地区授权、日期窗口、禁播时段、节目冷却、赞助商间隔、直播临时替换、实播对账与版权越界检查，并为县域发射台提供断网缓存单的离线回传批次处理。

## 离线回传批次

发射台断网时按缓存单播出，回网后补回传。回传处理遵循以下规则：

- **每台按包号只收一次**：`(station_id, package_no)` 唯一，重复提交同一包号不会重复入账。
- **校验失败保留整包待核**：任一段校验失败，整包状态为 `pending`（待核），失败段记录原因。
- **重试只续缺失段**：重试时只处理尚未入账（没有 `playout_log_id`）的段，已入账的实播不重复新增。
- **编排改动立即作废并重算**：新增排期、替换排期、追加地区授权等编排改动后，待核包立即作废（记录 `voided` 事件），并按当前节目单和授权快照重算（记录 `recalculated` 事件）。
- **已播段按播出时刻快照判越权**：授权快照在段入账时冻结，之后授权窗口变化不追溯已播段。
- **回传包、授权快照、对账列表同一结论**：已入账的段一定有实播记录，对账不会判为漏播；越权段的快照结论与对账的 `out_of_license` 异常一致。

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

测试覆盖完整流程：排期、临时替换、播放日志、按日期对账；同时覆盖时间重叠、未授权地区、实播错节目、离线回传批次的幂等接收、缺失段重试、编排改动作废重算、授权快照冻结与越权对账一致性等场景。

## 主要 API

- `GET /api/state`：节目、排期和最近对账异常
- `POST /api/programs`：创建节目并授权地区
- `POST /api/programs/{id}/regions`：追加地区授权
- `POST /api/schedule`：创建排期
- `POST /api/slots/{id}/replace`：替换计划节目并重新校验
- `POST /api/playout`：登记实播记录
- `POST /api/reconcile`：按日期生成漏播、错播、时长偏差和超授权异常
- `GET /api/stations`：发射台列表
- `POST /api/stations`：创建发射台
- `POST /api/backhaul`：接收离线回传批次（同一发射台同一包号只收一次）
- `POST /api/backhaul/recalculate`：手动触发待核数据作废并重算
- `GET /api/backhaul`：回传批次列表（可按 `station_code`、`status` 过滤）
- `GET /api/backhaul/{id}`：回传批次详情（含段、事件、授权快照）
- `GET /api/authorization-snapshots`：授权快照列表（可按 `air_date`、`station_code` 过滤）

准备排期时填写 `air_date`、`start_time`、`program_id`、`region`。页面会直接显示校验错误，不会保存失败的排期。
