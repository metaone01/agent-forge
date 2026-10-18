# Agent Forge 查询服务交付目录

目录包含服务代码、必要 tools 模块、四份 Schema、锁定的生产 requirements、summary 站点及公开三文件快照。不包含个人凭据、私有 state、canonical 源目录、插件文件或项目虚拟环境。交付校验不是来源认证；delivery.json 的 SHA-256 必须来自可信交付记录。

## 校验和环境

在交付目录之外准备 Python 3.10+ 专用环境，避免虚拟环境改变完整文件集。把下文 python 替换为该环境的实际解释器：

```sh
export PYTHONDONTWRITEBYTECODE=1
python -m pip install --require-hashes -r deploy/query-service/requirements.txt
python tools/package_query_service.py verify --directory . --expected-delivery-sha256 <可信交付记录中的SHA256>
```

快照 revision：@@REVISION@@，包/版本：@@PACKAGES@@/@@VERSIONS@@。bundle SHA-256：@@BUNDLE_SHA256@@。

## 首次导入与显式激活

以下命令写入与交付目录分离的新 public runtime，需由操作人确认后运行；查询进程不运行这些命令。首次激活的 expected-pointer 为 absent。已有 runtime 更新/回滚应先 snapshot-status，使用当时真实指针 hash；不能删除 current.json 绕过冲突。

```sh
python -m agent_forge_service.worker snapshot-import --runtime /srv/agent-forge/public --bundle bundle --expected-bundle-sha256 @@BUNDLE_SHA256@@
python -m agent_forge_service.worker snapshot-status --runtime /srv/agent-forge/public
python -m agent_forge_service.worker snapshot-activate --runtime /srv/agent-forge/public --revision @@REVISION@@ --expected-manifest-sha256 @@MANIFEST_SHA256@@ --expected-pointer-sha256 absent
python -m agent_forge_service.preflight --catalog . --runtime /srv/agent-forge/public
python -m agent_forge_service.app --catalog . --runtime /srv/agent-forge/public --host 127.0.0.1 --port 8791
```

API 只读 public runtime，无需 canonical 或 gh。采集 Worker 和 gh 留在授权本机。下载量不是人数，仓库发行不是插件版本，来源精选不是质量背书。

## Linux 和容器

systemd/nginx 样例在 deploy/query-service。目标主机需配置路径、专用服务用户、读权限、域名与证书，静态根目录只指向 site，不暴露 bundle/runtime/代码。API 用户只能读公开 runtime，与采集私有目录隔离。

容器配方需要明确提供审查过并按 digest 固定的 Debian 系 Python 3.10+ 基础镜像：

```sh
docker build -f deploy/Dockerfile --build-arg PYTHON_IMAGE=<已核实的Python基础镜像@sha256:摘要> -t agent-forge-query:<交付版本> .
```

.dockerignore 与 deploy/Dockerfile.dockerignore 仅允许代码、Schema 和依赖配置进入 API 镜像，不打入 site 或快照。后者绑定该 Dockerfile，从仓库根目录构建时也限制上下文。deploy/compose.yaml 要求设置已构建镜像 digest 和 public runtime 绝对路径。服务为非 root、只读根文件系统、只读 runtime，仅映射主机 loopback。容器先执行完整预检再监听容器内部接口。配方未在缺少 Docker 的构建机器上执行，不能作为 Linux、容器、TLS 或生产验证。

## 更新与回滚

先校验和导入新 bundle，再凭当前指针 hash 显式激活。旧 revision 保留以支持旧 cursor，回滚使用旧 manifest hash 与当前指针 hash。代码和站点在独立版本目录准备、验收后再切换，不能覆盖运行中的数据库。私有 state 灾备不在此公开目录内。

构建失败的输出可能保留，但没有完整 delivery.json；不能部署这样的目录。正式交付保持只读；严格文件集验证会拒绝 .venv、__pycache__、额外 state、缺失文件与未列出的内容。运行可设置 PYTHONDONTWRITEBYTECODE=1，防止 Python 字节码改变交付文件集。


## 按 Agent/type 导出规范元数据

可从已导入的公开runtime导出选定范围，无需private state/canonical或上游网络。输出必须是runtime之外的新目录：

```sh
python -m agent_forge_service.worker offline-export --runtime /srv/agent-forge --agent dsh --type plugin --output /tmp/agent-forge-offline
python -m agent_forge_service.worker offline-verify --runtime /srv/agent-forge --directory /tmp/agent-forge-offline --expected-export-sha256 EXPORT_SHA256_FROM_RECEIPT
```

将EXPORT_SHA256_FROM_RECEIPT替换为导出回执的exportSha256，并通过可信通道传递。可用--revision显式固定源版本；默认current只解析一次。导出保留符合范围的历史规范记录和first_seen，不重新推断最新版本。两文件offline格式只含规范元数据，不含Star/下载/推荐观察或候选，不是可激活的在线query-snapshot；完整公开情报继续用本交付内的bundle。SQLite读取时检查selected_status，unknown或not_in_scope对应的selected_version为null。


## 首个服务版本与初始化

项目版本保持0.1.0；state、公开query和批次配置均为首版格式1。公开快照的manifest与SQLite metadata另以storageContract=agent-forge-query-snapshot识别格式；Worker state使用agent-forge-worker-state。原规范元数据仍为Schema v2，插件自身版本不变。

开发期间旧state/query及测试交付仅为历史证据，不提供逐级升级或兼容转换；不要只改user_version。请用新runtime导入本交付中的当前格式bundle，或在本地从canonical初始化。当前受支持格式内的旧revision、cursor和回滚继续有效。未来真实发布后，只有必要的不兼容格式变化才增加版本并设计受控迁移。


## 快照保留与可选清理

快照默认在退役后保护游标24小时，再额外等待60秒并检查活跃读取；当前revision及最近N份保留。窗口结束后即使文件还在，旧游标仍返回410。清理不改current、private state或canonical。参数--snapshot-cursor-ttl-seconds只用于登记/启用；较短新参数不缩短已有保护。

先审查计划，输出文件须在运行目录和代码目录之外：

```sh
python -m agent_forge_service.worker retention-plan --runtime /srv/agent-forge/public --keep-last 3 --max-remove 3 --output /tmp/agent-forge-cleanup.json
python -m agent_forge_service.worker retention-apply --runtime /srv/agent-forge/public --plan /tmp/agent-forge-cleanup.json --expected-plan-sha256 PLAN_SHA256_FROM_RECEIPT
python -m agent_forge_service.worker retention-run --runtime /srv/agent-forge/public --keep-last 3 --max-remove 3
```

将PLAN_SHA256_FROM_RECEIPT替换为第一条回执。最后一条默认只预览；显式--apply才自动生成并应用当次计划。一次最多清理配置数量的已验证目录；读者占用或部分失败返回complete=false、pending和退出3，持续错误不忽略。隔离残留可由新计划处理，未知文件不递归删除。

附带的agent-forge-retention.service/.timer仅是未安装、未启用的小时任务模板，明确使用--apply。启用前审查参数、用户名、路径和权限；本计划没有安装定时任务或外部部署。模板使用独立写者agent-forge-publisher与共享只读组agent-forge-query、UMask=0027，public目录应为0750、文件0640。发布/import应保持相同组与umask，API仍只读；私有state与gh/secrets保持单独限制。当前Windows验证不代表Linux/systemd或POSIX租约实测。


## 本机 Worker 空间状态

```sh
python -m agent_forge_service.worker storage-status --runtime /srv/agent-forge/worker
```

该命令只读stat/disk_usage，不打开或创建private state；空间不足返回allowed=false/原因及退出3，不回收任何事实。定时采集默认在state.db/WAL/SHM合计达到1GiB或盘余量低于512MiB时暂停新请求，参数可显式调整。已发送请求和实际写入仍可能失败，该检查不是文件系统配额。公开API不依赖私有状态，继续查询最后成功revision。

可丢弃HTTP缓存默认最多1,024条、32MiB UTF-8逻辑值、闲置7天；淘汰validator后下次发完整GET，304不推进observed_at。历史观察/证据、首次发现、任务、限流和来源账本不删除；缓存腾页不等于SQLite文件立刻缩小。没有自动运行VACUUM、删除日志或安装定时任务。

## 运行日志

API 默认写固定字段的 stderr JSONL，关闭原始 Uvicorn access log，不输出原始 URL/query、动态包 ID、IP、认证头、Cookie、正文和异常文本。可选 --log-directory 指向 code/catalog/runtime 之外的独立目录，默认单文件1MiB、3个备份、7天；参数 --log-max-bytes/--log-backups/--log-max-age-seconds 可调整。按最早记录时间在启动或写入事件时整文件到期，不会因持续追加延长旧记录期限；停机/完全闲置时不另设后台删除任务。

systemd 示例使用独立 LogsDirectory，公开快照保持只读。compose 示例限制容器控制台文件为1m/4份；其他宿主控制台须自行设保留策略。nginx 示例关闭原始 access/error 请求日志，API固定字段用于请求诊断；如需网关诊断须另设脱敏和容量控制。模板尚未实际启动或部署。

日志目录拒绝接管未归属同名文件，保留外部内容，不清理事实/观察/证据。若已有单文件超过调低后的新预算会拒绝启动，需先审阅或改用新日志目录。文件写入失败后安全回退到stderr，不影响查询；stderr的保留由宿主负责。Worker显式命令的stdout仍是操作结果，不能把完整命令输出误当匿名访问日志永久追加保存。

## 私有Worker刷新覆盖

只有私有Worker runtime才能核对采集覆盖；纯公开接收端没有这些事实。提供实际在用、已审阅的policy文件后可只读查询：

```sh
python -m agent_forge_service.worker refresh-status --runtime /srv/agent-forge/worker --policy /etc/agent-forge/approved-policy.json
```

该命令不采集、不规划、不创建缺失runtime。最近成功200/304检查与真实观察时间分别计数；空目标覆盖为null，待解析/无链接包仍显示缺口，catalogCoverage不承诺全目录。groups有交叉，不能加总作独立仓库数。每日刷新预算须覆盖普通组每日一次及快组每slot一次，combined_refresh_target_exceeds_budget指出合计下界缺口；预算充足仍不保证请求/时限/来源覆盖成功，不会自动提高请求预算。默认5秒读取期限，超时失败，不输出部分成功。

可选--at仅限制检查与观察的历史截止，成员/分层/到期时间/映射仍为当前state，不能当作历史每日覆盖证明。示例policy仍需另行提供，交付不会偷偷开启采集通道或迁移旧开发数据库。
