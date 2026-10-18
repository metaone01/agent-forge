# manifest / Release 自动采集调度

当前服务格式统一为首版1；版本规则见[首版格式决策](../docs/decisions/0002-initial-service-formats.zh-CN.md)。

日期：2026-10-14，Asia/Shanghai。本模块把既有无凭据 Git 元数据扫描接入统一批次与 UTC 定时驱动，复用私有 state 首版格式1/公开 query 首版格式1，不新增数据库或迁移。没有安装新依赖、运行上游代码、下载插件二进制、自动采纳规范记录或部署长期任务。

## 显式配置与范围

batch policy首版格式1提供可选manifests/releases；downloads/recommendations/manifests/releases缺省或null表示停用。policyId参数不可改写，调整参数须使用新policyId。示例仅为明确预算的本地试点，不自动生效。

~~~json
{
  "manifests": {
    "budget": 12, "maxScans": 2, "intervalHours": 24, "retryHours": 8,
    "profiles": [
      {"path": ".claude-plugin/plugin.json", "kind": "claude_plugin", "agent": "claude-code"}
    ]
  },
  "releases": {
    "budget": 6, "maxScans": 2, "intervalHours": 24, "retryHours": 8, "maxPages": 3
  }
}
~~~

预算是每个 UTC 4h/8h slot 的 GET 上限，maxScans 是当次可选扫描范围数，二者必须同时启用或为0。预算在0–1000、范围数在0–100、manifest profiles在1–20、Release分页上限在1–10；检查/失败重试间隔在4–168小时且对齐slot。配置不借用普通仓库/搜索/npm/精选列表的预算。示例预算与间隔仍需试运行，不承诺全目录每日覆盖。

manifest 的候选目标是当时已知 repositories 与 discovery_candidates 的稳定仓库 ID 并集，按显式 path/kind/agent profile 检查。新增发现于下一次批次规划纳入；同slot已有批次幂等重放，不重新扩容。profile 只选择支持的声明格式与关联 Agent，不证明仓库包含插件或兼容该 Agent，不对任意 README/目录猜插件。当前支持 claude_plugin 的 .claude-plugin/plugin.json 和 agent_skill 的 SKILL.md 路径规则，沿用原有声明校验。

Release 只检查已经存在公开 repositories 观察的稳定 ID；仅发现而未观察的仓库不会被自动加入 Release 通道。仓库 Release/tag/asset 的公开字段是仓库级事实，不能推断 monorepo 中某个插件版本或下载人数；不抓取 Release prose、下载地址或插件文件，也不自动创建规范版本。公开发行观察与精确分发URL关联现已接入，见 [RELEASE-OBSERVATIONS.zh-CN.md](RELEASE-OBSERVATIONS.zh-CN.md)；其他包版本映射/选择策略仍待完成。

## 恢复、去重与公平轮巡

规划按任务目标的确定性 ID 轮巡，metadata 只保存各策略/通道的 cursor 与 lastPlannedAt，不保存每个仓库的复制状态。目标可能动态增加，规划对每条通道的scan历史只遍历一次，避免逐仓库重复扫描全部历史；轮巡在完整到期集合上继续，延后项不会因已有长扫描反复优先而永久饥饿。轮巡是内部采集选择，不能当公众推荐/质量排序。

既有 scan 表决定最新结束时刻与到期：manifest complete/not_found/rejected 是已完成检查，按 intervalHours 复查；failed 用 retryHours。not_found 不把仓库分类成插件，rejected 不等于上游代码不安全。Release 只有 traversal_complete 算当次遍历完成，partial/failed 用 retryHours；可变分页即使完成也不构成快照或删除证明。按slot对齐意味着迟到完成后的实际间隔可能短于配置小时数，不能声称每次读取严格至少间隔24h。

存在同目标 queued/running 扫描时复用原扫描、原Git对象进度与任务，不创建重复链；只运行本批选定扫描的任务，动态后续步骤继续受同一批预算限制，不领取无关手工扫描。已有 Release 活动扫描的 maxPages 与配置不同会明确 scan_policy_conflict/409 并回滚本次规划，不能偷偷改变上限、重复创建扫描或扩大请求范围。已结束的手工检查也用于到期去重。

每次 GET 前沿用持久 batch_attempts 预算预留；进程在确认前退出时保留未知收费，不退款。跨slot重新选中原未完成扫描时使用新slot的明确额度，旧请求/进度不重跑。任务已终态失败会在规划/执行后派生为 scan failed；不会因无待办任务而将 Release partial 说成 selectedWorkSettled=true。status 的 scanStates 单独披露扫描结果。

remainingPathRequestsWithoutRetries 只估计当前进度到完整路径的正常请求数，不含重试；manifest 根文件完整链5GET，每个目录层额外1GET，也可能在路径缺失/不支持时提前停止。Release 估计为原扫描剩余页上限，可能提前遇到短页。容量警告说明所选完整路径可能超出本slot额度，不能把这些估计当成含重试的容量保证。

## 额度、停用与定时发布

两条通道共享一个 AnonymousGitClient、无凭据身份与匿名core限流账本，独立于本机 gh 的已认证额度；403/Retry-After/退避沿用现有客户端规则。匿名额度受限时不切换到 gh 或认证 Contents/GraphQL。run_batch 返回 anonymousCore 的 nextNetworkAt，定时驱动据此有界等待；纯 manifest/Release 策略不需要伪造 gh identity，也不会创建 GitHubClient。

所有任务继续经过 [来源采集控制](SOURCE-CONTROLS.zh-CN.md)。提供方/仓库范围停用会阻止领取和预算预留，恢复继续原任务；已经开始的请求可以完成。scan maxScans 的轮巡不会把停用解释为上游不存在，partial/延期也不伪造成功覆盖。

~~~powershell
# 使用完整显式示例；不会因存在此文件而自动启动。
.\.venv\Scripts\python.exe -m agent_forge_service.worker batch-plan --runtime .\snapshots\query-service --policy .\agent_forge_service\examples\scheduler-scan-pilot.json

# 示例还含仓库/Topic/精选等认证通道时，identity仍须明确。
.\.venv\Scripts\python.exe -m agent_forge_service.worker schedule-tick --catalog . --runtime .\snapshots\query-service --policy .\agent_forge_service\examples\scheduler-scan-pilot.json --identity local-gh-label --max-requests 20 --max-batch-seconds 600 --poll-seconds 60
~~~

真实调度仍使用现有单 Worker 锁、deadline/停止信号、缺失slot合并、持久预算和原子发布。manifest 候选仅进入独立 discovery scope，仍需元数据分拣/正式采纳流程；规范包数不会因为成功扫描自行增加。collectionBatch 继续披露实际/预留/未知请求与部分发布，不声明全目录覆盖。

## 公开Release与资产观察（2026-10-14）

截止前成功Release页面现可随query 首版格式1发布独立可重放投影，feature按实际证据出现。GET /api/v1/releases 与 /api/v1/packages/<id>/releases 支持固定revision分页、指定规范版本、精确声明/仓库上下文筛选；新增只读release-query CLI与详情独立折叠面板。累计资产下载请求单列，不冒充人数/npm或插件版本；未知/缺页/partial/旧tag重用保持不确定性。接收端从原页与规范URL重新计算关联，未安装新依赖，不自动下载、采纳或认证安全/兼容性。规范目录latest_release排序尚未接入。完整字段、边界与验证见 [Release观察说明](RELEASE-OBSERVATIONS.zh-CN.md)。
