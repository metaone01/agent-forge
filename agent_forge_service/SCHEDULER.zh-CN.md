# UTC 定时采集与不可变发布

当前服务格式统一为首版1；版本规则见[首版格式决策](../docs/decisions/0002-initial-service-formats.zh-CN.md)。

本驱动复用 batches.py 的持久任务、通道预算、限流和租约，复用唯一快照构建器；所有写入使用现有 runtime 的 Worker 锁。API 继续只读公开快照，不触发采集。当前已验证本地夹具、SQLite 与恢复流程；未安装或启用系统定时任务，也未部署长期 Worker。

## 运行方式

先用现有 Python 环境进行不联网的单次检查。scheduler-offline.json 所有采集预算均为 0，仍会校验规范目录并构建快照；若用于完整目录，构建会消耗本机时间和存储。

~~~powershell
.\.venv\Scripts\python.exe -m agent_forge_service.worker schedule-tick --catalog . --runtime .\snapshots\scheduled-offline --policy .\agent_forge_service\examples\scheduler-offline.json --max-requests 1 --max-batch-seconds 60
.\.venv\Scripts\python.exe -m agent_forge_service.worker schedule-status --runtime .\snapshots\scheduled-offline --policy-id offline-20261012-8h-v1
~~~

需要真实采集时，复制 scheduler-pilot.json 到自己的配置路径并明确预算、Topics、发现起点和历史范围；示例 Top N、保护期及请求数是试点参数，不代表全目录覆盖或最终产品规则。配置的 policyId 一经持久化就不能用来修改策略；变更策略时使用新 ID，不删除旧事实或重置任务。

~~~powershell
.\.venv\Scripts\python.exe -m agent_forge_service.worker schedule-run --catalog . --runtime .\snapshots\scheduled-worker --policy .\agent_forge_service\examples\scheduler-pilot.json --identity local-readonly-gh --max-requests 10 --max-batch-seconds 1800 --poll-seconds 60
~~~

schedule-run 是前台进程。Ctrl+C、SIGINT 或 SIGTERM 设置停止标记。--identity 是用于隔离缓存与限流账本的非秘密标签，不会切换 gh 账号；实际请求使用本机 gh 已配置的账号。只执行公开元数据 GET，不通过更换标签绕过来源额度。仅下载通道或全部零预算时可以省略 identity；推荐通道也使用 GitHub core 额度，必须提供同一非秘密身份标签。

HTTP_PROXY/HTTPS_PROXY 可在启动进程前设置为运行者提供的代理地址，配置只对继承该环境的进程生效。驱动不写系统代理或修改 gh 登录。

## UTC 时段与时间语义

- 8h：UTC 00:00、08:00、16:00；在 UTC+8 是 08:00、16:00、次日 00:00，本地日期可能与 UTC 日期不同。
- 4h：UTC 00:00、04:00、08:00、12:00、16:00、20:00；需同时修改 slotHours 和 refresh.fastHours 并使用新 policyId。
- 首次启动或重启时立即处理当前时段。漏跑时段合并到最新时段，旧 pending/retry_wait/lease 仍由原队列恢复，不为每个漏跑时段突发重放网络请求。
- 每个时段规划一次。所选工作结算、请求预算耗尽、没有可继续工作或收集期限到达后发布；发布成功后，同一时段的调用不再 GET 或建库。
- 快组下次到期为成功所在 UTC slot 加 slotHours；普通组为成功所在 UTC slot 加 24h。成功发生在 08:00:07 时，次日到期是 08:00:00，避免晚几秒错过整批。last_success_at、指标观察时间与增长分母继续使用实际时间，不用时段时间替换原始采样事实。
- 系统时间倒退时返回 clock_wait，等待赶上持久时间；GET 已在途时仍先结算。发布后回执未能写入时，从已有目标恢复，不重新请求。

--max-requests 为一次 tick 的网络请求切片上限（1–100），整批的来源额度由 policy budgets 限制。--max-batch-seconds 必须为 1 到一个 slot 的秒数，从开始规划当前时段时计时，包含映射与规划时间。SQL/构建和已开始的 GET 不能按此期限强行中断；因此该值是开始新收集请求的期限，不是整个进程的硬执行时限。

--poll-seconds 为 1–60 秒。驱动同时参考任务 next_run、来源 nextNetworkAt、收集期限与 slot 边界；尚不可请求时不忙等，不向来源发空轮询。等待日志按状态去重，空闲等待仍会进行本地状态检查。内部等待以最多 1 秒的 Event 片段让 Windows 能分派 Python 信号；这些片段不额外触发 tick、SQL 或来源请求。

## 停止与崩溃恢复

每次 claim 前以及实际 fetch 前检查停止、收集期限与 slot 变化。claim 后尚未 GET 就停止时，释放租约并保留 attempts，预约的请求计为 0；已经 GET 则完成证据/任务结算后退出收集。发生崩溃且无法确认是否 GET 的 reservation 保留扣费，不退款或重置重试次数。

停止标记到达构建前会保留 publishing 阶段供下一次启动恢复。已经开始的快照构建继续结算；外部强制终止时仍依赖恢复路径，不承诺进程立即退出。

调度状态保存在现有 state.db metadata 的 scheduler:v1:<policyId>，没有第二个运行库，私有 state 当前 首版格式1、公开 query 仍 首版格式1；推荐到期状态也保存在既有 metadata，不新增运行库或迁移。状态包含 planning/collecting/publishing、策略 hash、最近发布与最多 20 条历史；拒绝重复 JSON key、错误类型、非法时间/路径/批次引用等。

- planning 失败：重启后重复校验映射并幂等规划。
- collecting 中断：同 slot 继续原批次，进入新 slot 后合并未完成队列。
- publishing 已有合法不可变目标：核验批次来源、DB/manifest hash 和 current 指针；目标尚未激活时用原 pointer hash 作条件切换。
- current 已是目标、回执未写：只补回执，不重建、不重新 GET。
- current 已被其他操作改变或目标内容变动：拒绝自动覆盖，保留证据供检查。
- .staging 残留：保留原目录并分配新 revision，最多记录 5 个废弃尝试；达到上限退出，不能反复无限创建目录。

不重用或修补已发布的不可变 revision。公开快照与私有 state 使用各自备份协议；恢复 publishing 状态时，所依赖的公开目标及原指针也需要按公开 bundle/启用流程恢复，不能把 private-state 备份理解为整个 runtime 的完整灾备。

结构性状态、策略、指针与 checksum 冲突使前台驱动退出；临时 ServiceError 按 poll 等待。普通 IO/构建错误交给进程退出与下一次启动恢复，不进行高频无限重试。

## 公开数据的完整性说明

manifest.collectionBatch 与 DB metadata 一致，发布器从持久批次账本核验来源和请求计数。它包含 scheduledSlot、实际 startedAt/plannedAt/collectionFinishedAt、completionReason、confirmedRequests、reservedRequests、uncertainAttempts 与 coalescedSlots。

selectedWorkSettled/partial 只描述本批选中的工作。catalogCoverage 始终为 not_guaranteed，deferredRefreshResources 描述规划推迟数；partial=false 不表示全目录、所有来源或每天刷新已达标。配额不足必须显示容量缺口，切换为 4h 不能替代容量核算。

当前自动通道覆盖已有 GitHub 仓库 metadata、Topics 增量/历史发现、明确 npm 下载，以及显式首版配置1 配置的 curated 推荐来源。显式首版配置1 的 manifest/Release 扫描现已接入自动预算/轮巡/恢复；Release/tag 到具体插件的版本映射、其他 Registry、更多治理、长期检查成功率和全目录覆盖仍待验收。新发现仍是候选，不自动采纳或改写规范目录。

## 进程托管示例

examples/agent-forge-worker.service 是 Linux systemd 配置样例，尚未安装或运行。需先配置真实用户、目录、现有 Python 环境与只读 gh 访问，再进行部署验收。它限制重启次数并为停止结算预留时间，不替代日志告警、权限验收或完整灾备。

## 来源精选的周期预算与公平调度

examples/scheduler-curated-pilot.json使用首版配置1，以显式recommendations字段启用精选来源；其他可选通道可为null。参数规范化后绑定policyId，不保留开发中间编码。

recommendations 包含 budget（每批 GET 上限，0–1000）、maxSources（每批选中来源上限，0–100）、intervalHours 与 retryHours（4–168 小时，必须为 slotHours 的整数倍）和 sources（最多 100 个完整来源定义）。budget/maxSources 同时为零才禁用；启用时必须配置来源。示例为每批 10 GET、最多 2 来源、24h 检查、失败后 8h 重试，均为未证明覆盖能力的试点。config/policyId 一旦持久化不能改参数，调整时使用新 ID。

来源 repositoryId/path/精选声明/标题范围/空表声明相同即为同一公开范围；label/解析数量预算别名只选一个稳定配置代表，合并为一次调度范围，不增加推荐票数。按最久未选中和到期时间轮巡，未完成扫描与新来源共享稀缺批次；不按 Agent、Star、下载或来源精选给插件增加隐含权重。来源数与完整列表解析均有上限，Git 根文件一次完整扫描至少 5 GET，每层目录多 1 GET；summary.minimumRequestsPerCompleteScan 披露完整链成本，remainingRequestsAtPlanning 按已完成步骤扣除，capacityWarnings 提醒单批剩余成本是否超额，dailyRequestBudget 是理论上限而非实际完成数。

同一 policy/slot 只规划一次；优先复用该范围已开始的 queued/running 扫描，保持原始 scan/slot、固定证据和队列 step，不重新起五步链。范围已被较新的完整手动检查满足时，不再续接其覆盖的旧扫描，旧扫描晚失败也不延后新成功检查的到期；未来 slot 的手动任务在到期前不纳入。终态失败保留旧成员，retryHours 之后可创建新扫描；未完成/限流等待保留原任务，nextRunAt 与共享 core nextNetworkAt 负责等待，失败不推进 lastSuccessAt。

按最新 selection_at 完整证据的实际 completed_at 推进到期，旧扫描晚完成不能延长较新成功时钟。成功下次到期为实际 completed_at 所在 UTC slot 加 intervalHours，终态失败到期为 finished_at 所在 slot 加 retryHours；原始成功与检查时间仍为实际时刻。slot 对齐是批次约定，不能将 intervalHours 说成任意迟到情况下严格间隔至少这些小时。内部状态在 metadata 的 recommendation-refresh:v1:<policyId> 保存源范围的 nextDueAt/lastSuccessAt/lastAttemptAt；严格拒绝非法 JSON、来源集变动和时钟倒退，托管 private backup 原样包含这些字段。

执行将 recommendations 加入既有跨通道持久轮转。RecommendationClient 仍使用独立精选表示/字节限制，但与普通 GitHubClient 必须共享 Worker 连接和相同 identity/core 限流账本；不得通过独立账号标签绕过额度。每一步 GET 在 batch_attempts 预留后执行，动态后续步骤继续计费；崩溃结果未知仍扣费，反复执行或重启不刷新额度。停止标记抑制新 GET，已经开始的请求先结算。预算只能完成前三步时可先发布 partial，公开来源仍只含 cutoff 前完整证据；后续时段续接剩余步骤，完成后才出现新的公开成员。

~~~powershell
# 仅在明确配置来源/预算、现有 gh 公共读取和部署环境后启动。
.\.venv\Scripts\python.exe -m agent_forge_service.worker schedule-tick --catalog . --runtime .\snapshots\scheduled-curated --policy .\agent_forge_service\examples\scheduler-curated-pilot.json --identity local-readonly-gh --max-requests 10 --max-batch-seconds 1800
.\.venv\Scripts\python.exe -m agent_forge_service.worker batch-status --runtime .\snapshots\scheduled-curated --limit 2
~~~

来源采集控制会在领取任务和批次预算预留前阻止匹配停用范围；保留待办/重试事实，不把停用当成已完成覆盖。控制 CLI 与每次 scheduled_tick 共享单 Worker 锁；运行期间遇到 worker_busy 时，应在本次 tick 完成后重试，不强制终止请求。规则见 [SOURCE-CONTROLS.zh-CN.md](SOURCE-CONTROLS.zh-CN.md)。

## 显式 manifest / Release 周期通道（2026-10-14）

batch policy首版配置1提供可选manifests/releases，不自动开启通道。扫描目标来自已知稳定仓库与显式声明 profile，Release只用已观察仓库；公平轮巡、旧扫描复用、正常/失败检查间隔、动态后续任务与持久预算、共享匿名core限流、来源停用和有界取消已接入定时驱动。私有state/query均为首版格式1。manifest成功仍是待审候选，仓库Release不猜插件版本，不下载文件或使用gh凭据访问Git对象。完整配置、估计边界与恢复证据见 [扫描调度说明](SCAN-SCHEDULING.zh-CN.md)。
