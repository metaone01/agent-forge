# 来源采集控制

当前服务格式统一为首版1；版本规则见[首版格式决策](../docs/decisions/0002-initial-service-formats.zh-CN.md)。

日期：2026-10-14，Asia/Shanghai。控制 Worker 后续的采集任务，保留公开事实、目录可见性与来源精选成员历史。它是本地运行者的采集选择，不是插件质量、安全、兼容性或使用人数判断，也不是精选来源的撤回声明。

## 范围和规则

| kind | 显式字段 | 生效范围 |
| --- | --- | --- |
| provider | provider=github.com 或 npmjs.org | 对应提供方的全部现有采集通道 |
| github_repository | repositoryId | 稳定仓库 ID 的元数据、声明、Release、精选扫描；已观测名称关联也受控 |
| github_repository_name | fullName | 显式名称的仓库请求，以及当前已知名称对应的 ID 扫描 |
| github_topic | topic | 此 Topic 的增量/历史发现，不改写既有发现记录 |
| npm_package | package | 此 npm 包的下载观察 |
| curated_file | repositoryId、path | 此仓库 Markdown 文件的全部配置别名，不依赖标签或解析预算 |

任何一个匹配范围停用都会阻止领取任务；恢复单项不能覆盖提供方/仓库等其他范围的停用。没有记录的范围默认开启。GitHub 仓库 ID 控制不对尚未解析的陌生名称猜身份：名称请求只有存在观测关联时才会被该 ID 规则阻止；要禁止首次名称解析，应添加显式名称控制或提供方控制。名称记录也不证明仓库所有权或永久身份。

JobQueue 保存各采集任务与提供方/具体资源的关联。首次记录控制时，为旧待办任务在同一事务内补齐关联；新任务在 enqueue 时保存。停用不删除、取消、标失败或重置任务，不修改尝试次数/firstSeen/checkpoint/指标。批次检查任务资格后才预留 GET 预算，被暂停任务不收费、不把缺少采集说成每日覆盖已经完成。batch-status 的 collectionPausedJobs 披露待办/重试中的受控任务，nextRunAt 排除这些任务；任务仍保留原状态，selectedWorkSettled 不会因停用而变为 true。

已经领取并开始的请求可以完成并留下实际事实。单 Worker 锁将 CLI 决策与普通 Worker 写入串行化；不承诺撤销已经发出的网络请求。未来恢复沿用原任务/游标/剩余预算，恢复本身不是一次上游成功观测。其他非采集本地任务不因提供方控制暂停。

## 本地操作

当前私有state和公开query均为首版格式1，不迁移开发中间数据库。inspect只读、不创建state；record要求已初始化且非symlink的当前格式，并复用单Worker锁。公开HTTP无写入端点。

~~~powershell
$targetJson = '{"kind":"github_repository","repositoryId":123}'
.\.venv\Scripts\python.exe -m agent_forge_service.worker source-control-inspect --runtime .\snapshots\query-service --target-json $targetJson

# expected-head 使用 inspect 的 latestEvent.id；无历史时填 none。
.\.venv\Scripts\python.exe -m agent_forge_service.worker source-control-record --runtime .\snapshots\query-service --target-json $targetJson --expected-head none --action disable --reason-code operator_collection_pause --request-key pause-123-1 --operator-label local-operator

# 恢复必须使用最新事件 ID，不能继续填 none。
.\.venv\Scripts\python.exe -m agent_forge_service.worker source-control-record --runtime .\snapshots\query-service --target-json $targetJson --expected-head "替换为inspect中的事件ID" --action enable --reason-code operator_collection_resume --request-key resume-123-1 --operator-label local-operator

.\.venv\Scripts\python.exe -m agent_forge_service.worker source-controls-query --runtime .\snapshots\query-service --revision "替换为公开版本" --limit 20
~~~

control-record 使用 BEGIN IMMEDIATE 原子检查最新头与追加；固定 action/reason、CAS、幂等请求键与全局不倒退的记录时钟。重复状态拒绝；同一请求重放返回原事件，不追加事件或覆盖后续状态。错误包括 control_head_conflict、control_request_conflict、control_clock_wait、control_transition_invalid，均为明确 409。request-key/operator-label 必须是非秘密标识，已知 token 前缀拒绝；两者只留私有 state，不进入公共事件。

## 公开投影与查询

发布截止时间前存在事件时才生成 features.sourceControls=true、sourceControlCoverage 与 source_control_events 公共完整账本。没有事件的旧版本保持原表/DTO 契约；明确查询控制记录时返回 source_controls_unavailable/503，不能由此推断远端实例当前的控制状态。公开 authority 固定 local-worker-collection-control，只描述来源，不认证账号/角色。事件 ID 是公开字段的 SHA-256 完整性标识，不是操作者签名。

GET /api/v1/source-controls?revision=<版本>&limit=20 支持 cursor。先按完整快照的控制 target ID 排序，再分页；排序不是推荐或优先级。返回当前单项状态、最新事件、最多 20 条近期历史、完整历史数量/截断说明和覆盖统计。页游标绑定 revision/limit/policy，发布切换不移动旧页。公开状态只反映该发布截止时间；本地控制立即影响后续领取，前端要等新快照发布后才能看到变化，不提供实时管理状态。完整事件链仍在公开表中，接收端校验目标规范化、固定规则、序号连续、父链/动作交替、时间/cutoff、行/hash、精确 SQL/字段和统计；重新计算外层 checksum 也不能绕过。

停用不隐藏包、不改变 Star/下载事实、不把 source claim 从 active 改为 withdrawn、不触发 canonical 采纳。恢复不伪造最新采样或重新定义窗口，新鲜度仍按实际观测时间计算。查询端只读公开快照，不读取 Worker 私有 state；控制记录与目录事实分别展示。

前端目录提供独立折叠面板，未展开时只显示覆盖概要，不请求全部控制历史。展开后按 20 条分页，失败保留旧页并允许重试；支持过期游标提示、显式读取最新控制版本、语言/主题和 320px/390px。面板会显示自己使用的 revision；用户显式刷新控制面板可以独立于目录版本，语言切换不会回退。只有目录切到不同公开版本/详情时，自动上下文才重新固定；旧请求不能覆盖新上下文。
