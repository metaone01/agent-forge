# Agent Forge 查询服务

公开元数据索引：采集 Worker 维护 SQLite state，构建不可变查询快照；API 只读快照，前端提供搜索、筛选、排序、详情和来源推荐。服务不托管插件文件、不执行上游代码，也不把热度当质量或安全认证。

软件版本0.1.0。state/query/batch使用首版格式1，规范元数据沿用Schema v2；见[格式决策](../docs/decisions/0002-initial-service-formats.zh-CN.md)。

## 启动

完整操作见[启动说明](../README-DEPLOYMENT.md)。在仓库根目录使用已安装锁定HTTP依赖的Python环境：

```powershell
$python = Join-Path (Get-Location) '.venv\Scripts\python.exe'
$runtime = Join-Path (Get-Location) 'snapshots\worker'
& $python -m agent_forge_service.worker build --catalog . --runtime $runtime
& $python -m agent_forge_service.preflight --catalog . --runtime $runtime
& $python -m agent_forge_service.app --catalog . --runtime $runtime --host 127.0.0.1 --port 8791
```

在另一个终端启动同一runtime的周期采集。先审阅策略的来源、起始日期和各通道预算：

```powershell
& $python -m agent_forge_service.worker schedule-run --catalog . --runtime $runtime --policy .\agent_forge_service\examples\scheduler-pilot.json --identity local-gh --max-requests 10 --max-batch-seconds 1800 --poll-seconds 60
```

`local-gh`是额度账本标签，不是token或gh账户切换参数。GitHub transport仅GET；API不需要gh。`--max-requests`限制每个执行片段，整批预算由策略控制。进程关闭后停止采集，再次运行复用原state和队列。更新失败保留已发布快照；候选资源不会自动进入规范目录。

## 数据目录

```text
runtime/
  worker.lock
  state.db
  snapshots/
    current.json
    .staging/
    <revision>/
      catalog.db
      manifest.json
```

一个Worker是state唯一写入者。查询API只读公开快照；独立查询机器可仅导入公开bundle，不持有state、canonical或个人gh配置。不要覆盖运行中的catalog.db。runtime应与代码、canonical和公开静态根目录分开。

API就绪入口为`/health/ready`，查询为`/api/v1/packages`。站点需单独托管，静态根目录指向构建后的`site/`，同域`/api/v1/`反代到API；页面为`/catalog/`。API不会自动托管HTML。部署配置见[查询服务部署目录](../deploy/query-service/README.zh-CN.md)。

## 查询与事实边界

- Star、下载量、来源精选按来源/口径/时间独立保存；缺失为unknown，真实0保持0。
- Star增长和下载增长分榜，按实际区间计算。下载次数不是使用人数，滚动30日总量不直接作差当增量。
- 相关度需要搜索词；排序、筛选和分页在完整快照执行，游标绑定revision和完整参数。
- 仓库创建、首次收录、推送、规范更新和明确发行时间分别表达；仓库Release不推断插件版本。
- 新发现默认候选，正式收录走元数据审核；来源推荐是来源观点，不是本站安全或质量背书。

## 操作参考

| 主题 | 文档 |
| --- | --- |
| HTTP路由、参数、错误和分页 | [HTTP契约](HTTP-CONTRACT.zh-CN.md) |
| 周期采集、停止与恢复 | [调度器](SCHEDULER.zh-CN.md) |
| 发现候选与声明解析 | [发现](DISCOVERY.zh-CN.md) |
| 来源精选 | [推荐来源](RECOMMENDATIONS.zh-CN.md) |
| manifest与Release通道 | [扫描调度](SCAN-SCHEDULING.zh-CN.md) |
| Release事实与版本关联 | [发行观察](RELEASE-OBSERVATIONS.zh-CN.md) |
| 候选分拣 | [候选审核](CANDIDATE-REVIEWS.zh-CN.md) |
| 来源采集停用/恢复 | [来源控制](SOURCE-CONTROLS.zh-CN.md) |
| 快照保留与清理 | [保留规则](RETENTION.zh-CN.md) |
| Agent/type离线导出 | [离线格式](OFFLINE.zh-CN.md) |
| 采集空间与可丢弃缓存 | [资源预算](RESOURCE-BUDGET.zh-CN.md) |
| 私有刷新覆盖诊断 | [覆盖报告命令](REFRESH-COVERAGE.zh-CN.md) |
| 日志隐私与轮换 | [运行日志](OPERATIONAL-LOGGING.zh-CN.md) |

运行`python -m agent_forge_service.worker <command> --help`查看各命令参数。来源覆盖取决于批准配置和实际请求结果；partial/stale不表示没有资源或已经全目录刷新。

批次策略示例位于 `examples/batch-policy.pilot.json` 与 `examples/batch-policy.sources-pilot.json`（相对于本目录），由 `load_policy` 校验；仓库根目录 `examples/` 仅放规范元数据 Schema 示例。运行这些策略前仍须明确来源、预算与执行环境。
