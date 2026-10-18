# Agent Forge：启动采集与查询服务

当前软件版本 **0.1.0**。本期提供公开元数据采集、批次快照更新和可独立运行的搜索服务；服务使用 SQLite，不需要 PostgreSQL。插件文件仍从原来源下载，本项目只提供元数据和原始链接。

## 已有交付包：直接查询

独立交付包包含代码、前端、依赖清单与公开快照，不含个人 gh 配置、private state、插件文件或虚拟环境。按包内 **README-DEPLOYMENT.md** 完成校验、首次导入和启动；它已填入该包对应的 revision 与校验摘要。API 无需 gh 或 canonical 源目录。

- API：`/health/ready` 检查当前快照；`/api/v1/packages?q=搜索词` 查询数据。
- 前端：将包内 `site/` 作为静态站点根目录，同域 `/api/v1/` 转发到 API；访问 `/catalog/`。
- 现成代理模板：`deploy/query-service/nginx.conf.example`。只公开 `site/`，不要把项目根目录、bundle 或 runtime 作为静态根目录。
- 服务默认监听 `127.0.0.1:8791`。域名、TLS 和主机服务安装属于之后的实际部署操作，需按实际环境完成。

只启动 API 不会自动托管前端。前端的 API 地址由页面 `agent-forge-api-base` meta 配置，默认同域相对地址；不要通过 `file://` 打开页面。跨域部署时需同时配置页面 API 地址与 API 的明确 `--allow-origin`。

## 源码运行：建立首份快照

需要 Python 3.10+，以及项目锁定的 `http` 可选依赖。使用项目专用环境；已有 `.venv` 可直接使用。新环境可按 `pyproject.toml` / `uv.lock` 用 `uv sync --extra http` 准备；查询交付包使用其自带的固定哈希 requirements。以下为在仓库根目录执行的 PowerShell 示例，runtime路径可替换为自己的数据目录。

```powershell
$python = Join-Path (Get-Location) '.venv\Scripts\python.exe'
$runtime = Join-Path (Get-Location) 'snapshots\worker'
& $python -m agent_forge_service.worker build --catalog . --runtime $runtime
& $python -m agent_forge_service.preflight --catalog . --runtime $runtime
& $python -m agent_forge_service.app --catalog . --runtime $runtime --host 127.0.0.1 --port 8791
```

`build` 校验现有规范元数据，生成不可变 SQLite 快照并切换 current。runtime 应与代码和 sources 分开。API 只读快照，不能通过搜索请求触发采集。

## 启动周期采集

在另一个终端使用同一解释器与 runtime。先查看 `agent_forge_service/examples/scheduler-pilot.json`，确认来源、起始日期与请求预算，再运行：

```powershell
& $python -m agent_forge_service.worker schedule-run --catalog . --runtime $runtime --policy .\agent_forge_service\examples\scheduler-pilot.json --identity local-gh --max-requests 10 --max-batch-seconds 1800 --poll-seconds 60
```

这是前台持续进程，关闭后停止采集；再次运行继续原state与任务。默认示例以UTC 00/08/16时段驱动，完成采集/校验/构建后发布快照。`--max-requests 10` 是每次执行片段的请求上限，整批预算仍由policy各通道控制，不是“整批最多10次”。首次完整目录构建/映射可能耗时数分钟。

本机 `gh` 仅用于公开 GitHub GET 查询；`local-gh` 是额度账本的非秘密身份标签，不是token，也不会切换gh账户。需要有效的本机只读查询环境。网络需要本机代理时，可在该终端设置：

```powershell
$env:HTTP_PROXY = 'http://127.0.0.1:7897'
$env:HTTPS_PROXY = 'http://127.0.0.1:7897'
```

试点策略包括仓库观察、Topic发现与明确npm分发的下载统计；来源精选使用 `scheduler-curated-pilot.json` 的显式来源配置，manifest/Release扫描见 `scheduler-scan-pilot.json`。这些是可审阅的独立示例，按需组合到自己的policy并使用新policyId；不会自动启用全部来源或承诺全目录每天成功更新。推荐来源和下载口径单独保存，缺失保持unknown，下载次数不称为使用人数。

采集成功会发布新快照；来源失败/限流按队列退避，可发布带partial/stale标记的已有事实，构建失败保留上一份可读快照。新发现先进入候选区，不能未经审核变成规范收录。停止采集不会使已经发布的搜索服务失效。

## 在其他机器提供搜索

采集机器可导出公开bundle，再交给独立查询机器导入，API端无需任何个人gh凭据。先在本机生成新的输出目录：

```powershell
& $python -m agent_forge_service.worker snapshot-export --runtime $runtime --output ./snapshots/public-bundle
```

记录输出的bundleSha256，再按独立交付包README执行 `snapshot-import` / `snapshot-status` / `snapshot-activate`。更新时使用当前真实指针hash，首次才用 `absent`；不要覆盖运行中的catalog.db。传输和目标主机启动需在实际部署时另行执行，按实际环境配置传输和服务启动。

## 完成范围与进一步说明

数据成功采集后按批次更新快照；采集或构建失败时继续读取已发布数据。来源覆盖与新鲜度取决于实际配置、预算及成功观察，不能将新revision理解为所有来源都已刷新。

软件保持0.1.0；state/query/batch采用首版格式1，既有规范JSON为Schema v2。不为废弃开发中间版本提供逐级迁移；新安装使用当前格式新runtime，未来真实发布后的必要变更再设计迁移。

完整命令见[服务说明](agent_forge_service/README.zh-CN.md)，版本规则见[首版格式决策](docs/decisions/0002-initial-service-formats.zh-CN.md)。

与现有 main 服务共用主机时，保留 `server/` 服务及 `/api/` 路由；仅将 `/api/v1/` 转发到本服务。两个服务默认端口均为 8791，必须为新服务显式选择其他端口，并同步修改启动命令与代理 upstream。独立交付以 `/catalog/` 为查询入口，不包含旧服务及其数据库。
