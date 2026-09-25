# 合作办学风险联动

面向业务人员的纯服务端系统：30 余个合作项目并行推进时，单个签证、标准或师资
问题可能沿项目依赖影响多个招生批次。本服务登记项目依赖、风险事件、缓解措施
与责任方，按版本化规则计算风险传播范围与处置时限，让管理部门看到风险传播
链路而不是孤立告警。

代码按领域模型、应用服务、持久化与接口边界组织；时间、标识和外部输入通过
可替换端口接入（`RiskService(db_path, clock=...)`），以便稳定复现状态变化。
运行数据与本地配置不得写入源码目录。

## 架构

```
service_09252_007/
  projection.py  领域核心：事件重放 → 风险状态（传播、撤销、依赖环收敛）
  rules.py       版本化规则：传播深度/衰减/处置工作日数，按风险发生时刻选取
  calendar.py    工作日历：周末 + 地区节假日
  service.py     应用服务：事件导入、影响查询、豁免、措施认领、升级、复盘
  storage.py     持久化：SQLite（WAL）、线程本地连接、BEGIN IMMEDIATE 事务
  api.py         接口边界：标准库 HTTP JSON API
  main.py        入口：python3 -m service_09252_007.main --db risk.db --port 8080
```

### 关键语义

- **乱序/重复消息结果一致**：所有改变风险状态的消息写入 `events` 表
  （`message_id` 幂等去重），投影按 `(occurred_at, message_id)` 重放重建，
  与到达顺序无关；重建写入的时间戳取自事件本身，同一事件流重放结果逐字节一致。
- **派生风险按 (项目, 类别) 聚合**，`basis` 为所有能传播到该项目的基础风险
  集合。基础风险撤销后重放，只有 `basis` 为空的派生风险才被关闭
  （`close_reason=basis_empty`）；仍有其他依据的派生风险保持打开并更新依据。
- **版本化规则**：每条基础风险按其发生时刻生效的规则版本计算传播范围与
  处置时限，新规则不影响历史风险。
- **豁免**：人工确认（`approved_by`）且必须有明确有效期
  （`valid_from`/`valid_until`）；有效期内风险为 `exempt`、不计逾期，
  到期自动恢复暴露。
- **租约认领**：`BEGIN IMMEDIATE` + 部分唯一索引保证同一措施至多一个活跃
  租约；同一责任方重复认领幂等返回；租约持久化在 SQLite，重启后保留，
  过期租约在启动时与操作前惰性清理。

## 运行

```bash
python3 -m service_09252_007.main --db /tmp/risk.db --port 8080
```

## API 概览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/events/import` | 批量导入事件（message_id 幂等，乱序安全） |
| POST | `/projects` · `/dependencies` · `/rules` · `/holidays` | 登记项目/依赖/规则版本/地区节假日 |
| DELETE | `/dependencies/{id}` | 移除依赖 |
| POST | `/risks/raise` · `/risks/revoke` | 上报/撤销基础风险 |
| GET | `/risks` · `/risks/{key}` · `/risks/overdue` | 风险查询（含 effective_status、deadline、basis） |
| GET | `/projects/{id}/impact` | 项目受到的影响（基础 + 派生，含来源与下游） |
| GET | `/risks/{key}/propagation` | 基础风险的传播范围 / 派生风险的依据来源 |
| POST | `/exemptions` · `/exemptions/{id}/revoke` | 人工豁免（明确有效期）与撤销 |
| POST | `/measures` · `/measures/{id}/claim` · `/release` · `/complete` | 缓解措施登记、认领（租约）、释放、完成 |
| POST | `/risks/{key}/escalations` | 升级（级别自动递增或显式指定） |
| POST | `/reviews` | 复盘（根因、改进措施） |
| GET | `/audit` · `/leases` · `/exemptions` · `/measures` · `/rules` · `/holidays` | 运行态查询 |

示例：

```bash
curl -X POST localhost:8080/projects -d '{"project_id":"A","name":"中英项目","region":"CN","owner":"office-1"}'
curl -X POST localhost:8080/dependencies -d '{"dependency_id":"D1","upstream":"A","downstream":"B","kind":"shared-faculty"}'
curl -X POST localhost:8080/events/import -d '{"events":[{"message_id":"m-1","event_type":"risk_raised",
  "occurred_at":"2026-09-25T09:00:00+00:00",
  "payload":{"risk_id":"R1","project_id":"A","category":"visa","severity":5}}]}'
curl localhost:8080/projects/B/impact
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：依赖环收敛、并发认领（多线程竞争同一措施仅一人成功）、跨地区工作日
（国庆黄金周 vs 无节假日地区）、撤销只关闭无独立依据的派生风险、乱序/重复
消息收敛、版本化规则钉定、豁免有效期、租约与审计重启恢复、HTTP 端到端流程。

## 编译检查

```bash
python3 -m compileall -q service_09252_007 tests
```
