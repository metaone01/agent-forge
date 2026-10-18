# 私有刷新覆盖与每日预算

`refresh-status` 读取当前首版 Worker state，以成功检查的时间衡量当前仓库刷新目标；它不会采集、规划、补写任务、改变排名或创建缺失 runtime。API 接收端只有公开快照，没有 private state，不能用本命令推断真实采集覆盖。

```powershell
.\.venv\Scripts\python.exe -m agent_forge_service.worker refresh-status --runtime .\snapshots\worker --policy .\agent_forge_service\examples\scheduler-pilot.json
```

使用实际正在运行的明确 policy；相同 policyId 不能更改参数。示例 Top N、保护期、预算仍是试点，不是全目录每日更新承诺。命令输出单个 JSON 到 stdout，普通 CLI 不额外打印运行事件；需要文件诊断时沿用独立 `--log-directory` 参数。报告仅供受控本地运维，不加入公开 HTTP 端点，不暴露 gh 凭据或请求正文。

## 预算与实际覆盖分开

对去重后的当前 GitHub 仓库，设普通组数量为 O，快组数量为 F，slot 长度为 H 小时，每批刷新预算为 B。每日预算为 B×(24/H)，最低成功检查需求为 O+F×(24/H)。快组已是多个 Agent/type 榜单及新发现保护组的并集，不再次按组累计。

例如8小时、普通250个、快组100个、每批100请求：每日预算300，但最低需求550，每日至少缺250；仅比较普通250与预算300会漏报。新的 refreshCapacity/combined_refresh_target_exceeds_budget 会明确反映这个缺口。minimumAverageBudgetPerSlot 是 F+ceil(O/(24/H))，为平均预算下界，不是实际每批时限保证。

这些数值假设每次仓库检查只用一个GET、组成员/预算不变且所有请求成功；不含 bootstrap、发现、npm下载、manifest、Release、来源精选、失败重试、限流等待和发布。预算达标只是必要条件，不证明全部来源可覆盖；304也占一次请求。瞬时积压、长尾预算预留、网络/配额和发布耗时仍会妨碍目标。不能据此自动调高请求预算或把8小时改为4小时。

## 报告口径

- totals 对当前 active、归属指定 policy 的仓库去重；tiers 区分普通24h与快组slot时长，groups 按 Agent/type/tier 展示。组有交叉，不能把各组计数加总当总仓库数。空目标的 checkedCoverage 为 null，不报100%。
- checkedWithinTarget 只按截止时点之前成功200或304的 checked_at 计数，包含恰好窗口边界。失败/未检查不补零或推进成功时间；notCheckedWithinTarget 与 neverChecked 分开。
- observationWithinTarget 按 observed_at 衡量数值观察的新鲜度，304不制造新采样。最近检查成功不等于最近取得新的可比Star增长样本。
- dueNow 包含正好到期，overdue 仅早于当前截止；oldestCheckAgeHours 无成功检查时为 null。停用资源仍留在目标分母，避免隐藏未覆盖；本报告不另行猜测暂停原因。
- mapping 区分已有索引包、已扫描映射包、无GitHub链接包、待解析/冲突仓库名、已解析仓库及未规划目标。索引存在而映射尚未执行时也明确告警，不能把空映射当空目录的成功覆盖。
- `--at` 只限制成功检查/观察的历史截止；成员、分层、next_due_at 和映射仍来自当前私有状态，不是历史 cohort 回放。assignmentBasis 明示此边界，assignmentsAfterCutoff 标记规划晚于截止的情况，不能用此命令证明过去每天的完整覆盖。
- 报告默认5秒读取期限，SQLite进度处理器和Python迭代共同检查，超时失败而不是返回部分成功；分组数/会员JSON有固定上限。它在单个 mode=ro、query_only 读取事务中看到已提交WAL，不使用 immutable=1 忽略活跃WAL。stateWrites=0表示不写逻辑表；SQLite的常规只读WAL协调仍可能涉及sidecar，不承诺文件系统零副作用。

当前工作负载来自有限来源；仓库名未解析、没有明确分发、未成功采集的资源都保留缺失。catalogCoverage 始终为 not_guaranteed。真实持续运行的覆盖率需要跨多周期真实观察验证，不能用模拟时钟测试代替。

软件保持0.1.0，state/query/batch保持首版1、canonical保持Schema v2。新增报告不增加数据库表、索引、迁移或格式兼容分支。
