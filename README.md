# 无人机飞行计划审批与空域协调系统

标准库独立项目。系统记录运营方计划、航线、载荷、高度、人口风险和应急方案，检查临时禁飞区、高度范围、人口风险以及相邻有效计划冲突。审核结果支持离线编号幂等回传，计划变更会使原批准失效并生成通知。

多架无人机以编队为单位运行：建队即绑定成员计划，整队一次占用一个空域容量；任一成员改动时间或航线，全队批准作废并释放旧占用。两个调度员同时提交同一空域时只有一个能占满名额，另一个返回冲突成员与剩余容量。编队拆分或合并后重新核对冲突；救援队紧急插入顶出的编队保留原批准依据，容量腾出后可恢复。

## 运行

```bash
python3 app.py --db drone_airspace.db
```

默认监听 `127.0.0.1:8205`，首页 `/`，健康检查 `/health`。

身份头为 `X-User-Id`、`X-Role`；运营方还需 `X-Operator`。角色：`viewer`、`operator`、`airspace_reviewer`、`commander`、`auditor`。

## 主要接口

- `POST /api/restrictions`：新增临时限制或禁飞区。
- `POST /api/plans`：创建飞行计划。
- `GET /api/plans/{id}/check`：检查硬约束和相邻交通冲突。
- `POST /api/plans/{id}/submit`、`approve`、`reject`：提交和审核；审核使用 `offline_id` 保证断网重连幂等。
- `POST /api/plans/{id}/change`、`cancel`：版本化变更与取消，并生成通知。
- `GET /api/formations`、`POST /api/formations`：列出与组建编队（`member_plan_ids` 绑定成员，`leader_plan_id` 指定队长，`kind: "rescue"` 为救援队）。
- `GET /api/formations/{id}`：查看编队及成员。
- `POST /api/formations/{id}/submit`、`approve`：整队提交与整队批准；批准使用 `offline_id` 幂等。
- `POST /api/formations/{id}/split`：拆出成员另建新编队，原编队作废并重新核对冲突。
- `POST /api/formations/merge`：合并多个编队为新编队并重新核对冲突。
- `POST /api/formations/{id}/reinstate`：恢复被顶出的编队（保留原批准依据）。
- `GET /api/notifications`、`POST /api/expire`：通知与到期处理。
- `GET /api/state`：按角色返回计划、限制和公开信息。

## 编队容量与冲突

- 容量按编队计量：同一片重叠空域（航线包围盒缓冲 0.002 经纬度、时间相交）内同时运行的编队数不得超过 `formation_capacity`（默认 2，可由构造参数调整），与单机数量无关。
- 整队批准时若容量已满，返回 `409 formation_capacity_conflict`，`details` 含 `capacity`、`used`、`remaining_capacity` 及 `conflicting_formations`（每个冲突编队列出 `conflicting_members`）。
- 成员计划变更时间或航线、或被取消时，所在已批准/已提交编队整队作废（`invalidated`），成员退回 `draft`/`submitted`，旧容量释放。
- 计划属于编队时不能通过单机接口提交或批准，须走编队接口。
- 指挥官可凭 `override_reason` 以救援队名义紧急插入：容量满时顶出一个最新批准的重叠编队，被顶出编队状态为 `bumped`，其成员批准记录（`offline_id`、决定、审核人）完整保留；容量腾出后可 `reinstate` 恢复。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

空域几何使用经纬度矩形和航线包围盒近似，不包含多边形、椭球距离、地形、实时遥测和完整间隔标准。紧急授权只能覆盖空域及交通冲突，不能绕过载荷与高度硬限制。身份头、无签名离线审核以及单机 SQLite 适合原型，生产环境需要 PKI、真实 GIS 引擎和跨机构事件总线。
