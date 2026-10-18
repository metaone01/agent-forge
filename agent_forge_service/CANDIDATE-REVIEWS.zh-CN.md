# 候选元数据分拣记录

当前服务格式统一为首版1；版本规则见[首版格式决策](../docs/decisions/0002-initial-service-formats.zh-CN.md)。

日期：2026-10-13。候选自动分类和运行者元数据分拣相互独立。本模块记录运行者对目录收录的操作，保留规则、元数据视图指纹、记录顺序和旧决定；不会批准 canonical 收录、安装、安全、质量或兼容性。公开 API 继续只读，写入只通过拥有私有 runtime 的本地 Worker CLI。

## 操作与规则

| action | 公开状态 | 必需 reason-code | 含义 |
| --- | --- | --- | --- |
| request_metadata | needs_metadata | additional_metadata_required | 运行者要求补充元数据 |
| reject | rejected | outside_catalog_scope | 运行者认为候选不在当前目录收录范围 |
| unsupported | unsupported | unsupported_catalog_metadata | 当前目录不支持该元数据，保留候选与证据 |
| reopen | pending | reconsider_metadata | 重新待审，恢复到需要运行者进一步处理的状态 |

没有 approve、safe、quality_score 或自动采纳命令。默认查询保留全部候选，人工状态只在明确 reviewDisposition 筛选时限制集合；原 reviewStatus、issues、lifecycle、声明字段、首次发现和 canonical 不被改写。reopen 不修复缺失字段或身份冲突，也不跳过既有授权提交流程。来源停用/采集禁用现已通过独立控制记录接线，见 [SOURCE-CONTROLS.zh-CN.md](SOURCE-CONTROLS.zh-CN.md)；候选导出、canonical 采纳及远端审核角色验证仍是后续工作。

公开 authority 固定 local-worker-metadata-triage，明确表明来自本地运行者记录；没有认证远端 GitHub 账号或管理员角色。operator-label 与 request-key 只存在私有 state，不能当作公众信任标签。事件 ID/hash 与 checksum 用于一致性检查，不能单独认证操作者或上游来源。

## 元数据视图指纹与历史

candidate-metadata-review-v1 对候选身份、名称/必要摘要、类型/Agent 声明关联、自动分类/问题、仓库数字 ID/name/URL/Topics/archived/disabled/defaultBranch、选定声明字段与固定 commit/blob/content hash 构造规范 JSON 指纹。Star/fork 计数、读取时刻、与该候选无关的搜索页变化不改变分拣视图。仓库发现没有固定声明或代码 commit 证明；指纹不是插件代码审计。声明候选以固定 Git 对象绑定自己的上下文，新 commit 产生新候选，不继承旧记录。

写入需同时提供 inspect 的 expected-evidence-sha256 和 expected-head。BEGIN IMMEDIATE 保护检查与追加，指纹变动返回 review_evidence_changed/409，记录头变动为 review_head_conflict/409；时钟倒退拒绝。reopen 必须有匹配当前指纹的现有分拣记录，不允许首次或重复 reopen。

同 request-key 重试相同操作返回原回执，不再追加；参数变化为 review_request_conflict/409。元数据变更后再重放旧请求，也只返回原事件，不把它重新应用到新内容。公开状态 superseded 表示最后记录的指纹与当前视图不同，旧事件仍保留。每项展示最近 20 条，historyCount/historyTruncated 披露截断；完整公开事件和上下文保留在独立表中，接收端重放全部链验证顺序、父记录、操作和 cutoff。

## 本地 CLI

先检查现有候选，再由运行者选择操作。以下写入示例只说明参数，不表示已对任何真实候选执行操作：

~~~powershell
# 只读；不创建 state、不迁移旧 schema、不联网。
$review = (& .\.venv\Scripts\python.exe -m agent_forge_service.worker candidate-review-inspect --runtime .\worker-runtime --candidate-id "github-repository:123" --limit 20 | ConvertFrom-Json)

# 运行者决定需要补充元数据时，使用刚取得的指纹和记录头。
.\.venv\Scripts\python.exe -m agent_forge_service.worker candidate-review-record --runtime .\worker-runtime --candidate-id $review.candidateId --expected-evidence-sha256 $review.evidenceFingerprint --expected-head $review.expectedHead --action request_metadata --reason-code additional_metadata_required --request-key "my-operation-001" --operator-label "local-operator"

# 公开接收端查询不需要私有 state 或规范 checkout。
.\.venv\Scripts\python.exe -m agent_forge_service.worker query --runtime .\query-runtime --scope discovery --review-disposition needs_metadata --limit 20
~~~

操作者标签和请求键必须是非秘密的有界标识，不传 token、邮箱或自由文本评论。首次 expected-head 为 none，后续为最新事件 ID；重试同一操作保留原参数和 request-key。CLI record 要求已有、非 symlink 私有 state，并复用 Worker 锁；不通过 HTTP 提供写接口，不执行插件或修改 GitHub。

## 数据与迁移

私有state首版格式1初始化candidate_review_contexts与candidate_review_decisions；不升级开发中间库。备份仅校验/恢复当前格式。

公开 query 仍 首版格式1。只有 cutoff 前实际存在分拣事件时，发布 discoveryReviews feature、discoveryReviewCoverage 及 discovery_review_contexts/discovery_review_events；无事件快照保留旧 DTO/表契约。metadataReview 与机器 reviewStatus 独立。公开不包含 operator-label、request-key、credential identity 或私有任务。同一当前格式内无feature的revision保持可读；明确要求分拣筛选时返回 reviews_unavailable/503。

## 查询与界面

GET /api/v1/packages?scope=discovery&reviewDisposition=rejected 支持 not_recorded/rejected/unsupported/needs_metadata/pending/superseded，按完整候选集合先过滤后排序分页；字段与 review-policy/revision/limit 绑定 cursor/cache。catalog scope 下该参数或未知/重复值明确拒绝，不静默回退。过滤器不代表质量榜，也不自动隐藏被拒绝候选。

/catalog/ 在候选 scope 显示元数据分拣状态筛选；详情单独展示状态、指纹、操作与时间、当前匹配情况和完整性说明。旧快照禁用此选项并说明缺少能力，不能解释成“无人审核”。正式目录切换清空该筛选，保留 canonical 统计、版本与既有推荐边界。
