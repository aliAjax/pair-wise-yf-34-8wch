# 无人机飞行计划审批与空域协调系统

标准库独立项目。系统记录运营方计划、航线、载荷、高度、人口风险和应急方案，检查临时禁飞区、高度范围、人口风险以及相邻有效计划冲突。审核结果支持离线编号幂等回传，计划变更会使原批准失效并生成通知。

多架无人机可组成**巡检编队整队放行**：编队绑定成员计划、整队一次占用空域名额；任一成员改动时间或航线，整队批准立即作废并释放旧占用，须重新整队审批。容量仲裁在 SQLite `BEGIN IMMEDIATE` 事务内完成，两个调度员同时批准时只有一个编队占满名额，另一个收到 409 及冲突编队/剩余容量明细。拆分与合并会重新核对冲突；救援队经指挥官授权可顶出常规编队，被顶出编队保留原批准依据，容量恢复后可复队。

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
- `POST /api/capacity-windows`：按区域与时段配置空域名额窗口（审核员/指挥官）。
- `POST /api/formations`：建队并绑定成员（`name`、`plan_ids`、`leader_plan_id`），一架计划同一时刻只能属于一个编队。
- `POST /api/formations/{id}/members/add|remove`、`POST /api/formations/{id}/split`、`POST /api/formations/merge`、`POST /api/formations/{id}/dissolve`：成员维护、拆分、合并、解散。
- `POST /api/formations/{id}/submit`：整队提交，记录成员版本快照并把成员一并置为 submitted。
- `GET /api/formations/{id}/check`：整队冲突报告，含成员硬约束/空域冲突、提交后版本漂移成员、容量窗口缺口与每个窗口的 `slots / held_by_formations / solo_approved / needed / remaining / conflicting`。
- `POST /api/formations/{id}/approve`：整队审批，整队一次占满容量，并把批准记录挂到每个成员计划；`preempt=true` + 指挥官 `override_reason` 为救援队插队，按"最晚获批、非紧急"顺序顶出常规编队。
- `POST /api/formations/{id}/reinstate`：被顶出编队凭保留的原批准依据复队；成员有改动或拆并时返回 `basis_stale`，须重新整队审批。
- `GET /api/formations/{id}`：编队详情（含成员、原批准依据）；`/api/state` 同时返回 `formations` 与 `capacity_windows`。
- `GET /api/notifications`、`POST /api/expire`：通知与到期处理（编队到期同步释放名额）。

### 编队状态语义

`forming → submitted → approved → (displaced) → approved`；成员改动/取消、拆分、合并、解散会使整队批准作废（`invalidated`）并释放 `formation_slot_occupancy` 中的旧占用，原 `formation_approvals` 记录保留为历史依据。被救援队顶出时为 `displaced`，成员计划为 `displaced`，但批准依据与成员快照保留，复队时重新核对容量和成员版本。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

空域几何使用经纬度矩形和航线包围盒近似，不包含多边形、椭球距离、地形、实时遥测和完整间隔标准。容量按区域时间窗口内的人头数计，不做高度层、时段细分与窗口重叠拆分。紧急授权只能覆盖空域及交通冲突，不能绕过载荷与高度硬限制。身份头、无签名离线审核以及单机 SQLite（线程各自连接 + WAL 串行写事务）适合原型，生产环境需要 PKI、真实 GIS 引擎和跨机构事件总线。
