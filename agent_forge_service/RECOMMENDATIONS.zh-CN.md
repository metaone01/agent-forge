# 来源精选观察与撤回证据

当前服务格式统一为首版1；版本规则见[首版格式决策](../docs/decisions/0002-initial-service-formats.zh-CN.md)。

日期：2026-10-13。私有 Worker 已保存固定公开 Git 对象、完整来源快照与列表成员变化；recommendation_projection.py 从截止前完整证据重建公开查询快照，并已接入精确规范版本声明链接关联、HTTP 契约、CLI、bundle 接收校验与前端 transport。来源目录/成员与精确版本关联 UI 已通过真实 Edge；统一周期预算/公平轮巡已完成本地接线；长期运行、来源治理和真实 FastAPI HTTP 验收尚未完成。旧快照缺少 recommendations feature 时继续明确返回 recommendations_unavailable/503。

## 事实边界

当前 provider 是 github-curated-markdown-v1：只处理来源明确自称 curated list 的公开 Markdown 列表，不将任意链接、仓库 Topics、Star、Schema 合格或本站收录当作推荐。claimKind 固定为 curated_listing，表示该来源的精选成员关系，不表示本站推荐、安全认证、质量评分或确切插件身份。

来源定义明确指定稳定 repositoryId、字面文件路径、公开精选声明、开始/结束二级标题、条目数量边界与可选的公开空列表声明。源码文件或任意路径不作为 provider；GitHub 元数据查询也不执行 hooks、插件或 Markdown 中的命令。示例配置不会自动开启采集。

条目保存目标 GitHub HTTPS 链接、名称、所在行/类别及原链接；不收录条目后的文章描述或外部徽章。分类导航、代码示例和注释不作为成员；未知条目语法、截断 tree、路径缺失、边界/精选声明变化均不能报告完整快照。默认空列表失败；只有配置的公开 emptyStatement 在该范围明确出现，才接受完整空快照。

原始文件位置不是推荐排名，source_rank 始终为 null。仓库根、tree 路径、blob 文档路径保持独立目标；owner/repo 大小写与 README 导航锚点按 GitHub 身份规范化，原始链接保留为证据。尚不将源目标关联到 canonical 包，更不会将仓库级精选归因给同仓库的每个插件。

## 持久采集链

扫描依次读取公开数字仓库 ID、默认分支 commit、逐层非截断 Git tree、固定 blob，并再次验证该数字仓库的公开身份/别名。保存完整 commit/blob SHA、内容 SHA-256 与每步 GET 证据，只有成功终结扫描才能保存 recommendation_snapshots 和成员观察。

- 一个 invocation 最多 1–100 次实际查询；复用原有 JobQueue 租约、幂等任务与 fencing，单 Worker 使用原有 OS 锁。中断/预算不足可以下次继续，未完成扫描不发布声明。
- gh 只能调用固定 github.com 的 allowlist GET 端点，不使用 shell，不打印凭据、debug HTTP 或上游错误正文。profile identity 是非秘密的本地标签，必须与同账号其他 gh 通道一致，才能共享 core 限流与预算；gh 不会绕过 GitHub 额度。
- 缓存 representation 与匿名 manifest 通道隔离；304 验证缓存 hash，复用原始内容观察时间，推进公开分支的 checked_at。缓存缺失、传输失败、primary/secondary limit 保留持久重试状态。
- 列表文件最多 2 MiB、HTTP 响应最多 4 MiB、每次查询期限 30 秒、最多 10,000 条目。此范围仅用于明确配置的 curated 列表；manifest 原来的 128 KiB 限制保持。
- GitHub Contents 在大于 1 MiB 的文件上可能不给内容，因此扫描读取固定 Git blob，并验证 Git blob SHA 和 tree/blob 长度。raw body 与缓存保存在私有 state，不能随公开 query bundle 输出。

只对同一 source identity 的完整快照进行对比。存在成员产生 active，上一完整快照中的成员在新完整快照缺失时产生 withdrawn/complete_snapshot_absence，并关联前后两个快照。其含义仅为“该来源列表移除此目标”，不是插件下架或本站撤销认证。重新出现保留 active→withdrawn→active 历史。配置/范围改变产生新来源身份，不跨列表推断撤回。

比较时使用实际分支检查时间，保存实际完成时间。旧扫描迟到可以保存固定证据，但不能撤回已观察到的更新快照；未来公开投影必须同时检查 completed_at 截止，不能把较晚完成的扫描回填过去。抓取失败、分页/解析不完整或一次未命中不会产生撤回。

## 命令

~~~powershell
# 本地示例：只登记明确来源并排入持久任务，不立即联网。
.\.venv\Scripts\python.exe -m agent_forge_service.worker recommendation-enqueue --runtime .\recommendation-runtime --source-definition agent_forge_service/examples/recommendation-awesome-mcp.json --slot 2026-10-12T16:00:00Z

# 有界 GET；identity 使用其他 gh 通道相同的非秘密标签。
$env:HTTP_PROXY = "http://127.0.0.1:7897"
$env:HTTPS_PROXY = "http://127.0.0.1:7897"
.\.venv\Scripts\python.exe -m agent_forge_service.worker recommendation-run --runtime .\recommendation-runtime --identity local-gh-public-metadata --max-requests 5

# 只读状态；不会创建私有库，也不会升级旧 schema。
.\.venv\Scripts\python.exe -m agent_forge_service.worker recommendation-status --runtime .\recommendation-runtime --limit 20
~~~

请求间隔/限流可能使一次 invocation 只完成部分步骤；根据持久状态与 nextNetworkAt 再运行，不放宽配额。默认根目录文件至少 5 次成功 GET；每层子目录另需一次 tree 查询。status 不输出整个成员清单、README 或 credential identity。

## 私有 schema 与恢复

Worker state首版格式1初始化全部recommendation表与索引；公开query首版格式1通过recommendations feature声明curated投影及覆盖。私有backup只校验/恢复当前格式，不迁移开发中间库，不将私有正文作为公共查询数据。

当前只支持首版格式1的初始化、备份与恢复；开发中间数据库保留作证据，不提供升级或转换。

## 公开投影与查询

公开 policy 为 public-curated-projection-v1。构建只接受 completed_at、selection_at 与 scan.finished_at 均在 cutoff 前的完整五步 Git 证据，重新核验固定对象/hash/路径并解析保存的私有 blob，不直接相信可变观察表的当前状态。公开 snapshot 含来源范围、commit/blob/content hash、请求与时间摘要、成员引用，不含 README 正文、完整 tree、任务、凭据或私有身份。checksum 证明收到的内容一致，不能单独认证上游来源；bundle 接收仍要求独立可信的描述 checksum。

同一 repositoryId/path/精选声明/标题范围/空表声明表示同一公开来源；label 与条目预算仅为配置别名，不能增加独立推荐来源数。同一 selectionAt 指向不同 commit/content 的来源证据拒绝，不按 ID 任意挑选。旧扫描迟到保留证据但不回退当前指针；历史 revision 按完整完成截止重建，不能补入随后才完成的扫描。

关联由公开 package_versions 的精确 canonical JSON 重算：仅 /links/repository 和 git/github-repo 分发 URL 的规范目标完全相同才建立 exact-declared-link。仓库根不匹配任意 tree/blob 子插件，仓库上下文与分发声明分别披露；保存 version、canonicalHash 与字段引用，endorsement 为 not-inferred。来源成员关系不是插件背书，同仓库多包也不能多算独立推荐。指定 version 必须精确命中，默认 canonicalLatest，不借 observedLatest 或字符串排序回退。

- /api/v1/recommendation-sources：revision、limit、cursor；来源目录、范围、最后成功/观察与动态 stale。
- /api/v1/recommendations：必需 source，可选 revision、limit、cursor、status=all/active/withdrawn；浏览来源成员，不暗示规范包身份。
- /api/v1/packages/{id}/recommendations：可选 source、version、status、revision、limit、cursor；仅该精确规范版本的声明链接关联。

分页按 source-target 字母序，游标绑定 endpoint、package/source/status/version/limit/policy/revision。sourceRank 全为 null；字母序不是推荐排名，不支持 source_rank 排序。空响应 availability=unknown，不能断言全网没有推荐。超过 24h 未成功检查展示 stale，但不因时间流逝擅自撤回成员。引用最多返回 20 条，citationCount/citationsTruncated 明确披露；撤回包含前后完整 snapshot 与旧成员引用。

只读 CLI 不要求私有 state，不触发联网：

~~~powershell
.\.venv\Scripts\python.exe -m agent_forge_service.worker recommendation-sources --runtime .\query-runtime --limit 20
.\.venv\Scripts\python.exe -m agent_forge_service.worker recommendation-query --runtime .\query-runtime --id "pkg/id" --version "1.0.0" --status all --limit 20
# 从上一命令取得 source ID，再按来源浏览：
.\.venv\Scripts\python.exe -m agent_forge_service.worker recommendation-query --runtime .\query-runtime --source "curated-source:<64位hash>" --limit 20
~~~

## 来源精选界面

/catalog/ 顶部“浏览精选来源”进入来源目录，来源链接固定当前 revision 并分页浏览成员；可选择 all/active/withdrawn。包详情独立显示当前精确规范版本的声明链接关联，切换版本不能回退到 canonicalLatest。来源字母序不代表推荐排名；空事实为 unknown，stale 不自动撤回，来源成员不意味着已收录或可安装的规范插件。

UI 展示前后完整证据和最多 20 条行引用、实际检查/观察时间与省略数；翻页失败保留原结果和页码，重试或主动读取最新，最近 64 页支持回退。详情/版本切换取消旧精选请求，迟到响应不能覆盖新页。新增脚本 recommendations-ui.js 在 api-catalog.js 前加载；页面与 assets 必须随同一部署产物更新。

## 周期采集接线（2026-10-13）

首版batch policy配置1 支持显式 recommendations 配置，固定来源与 GET/来源数/刷新/失败重试上限；来源定义别名合并，最久未选中范围优先，未完成扫描保持原任务并跨时段恢复。较新的完整手动检查可以满足来源到期，不重复发 GET。每一步动态请求计入原持久 batch_attempts 预算，并与 GitHub 其他通道共享同一 identity/core 限流账本；额度不足和未知崩溃扣费不会因重启退款。

24h/8h 只是可配置试点，成功或失败所在 UTC slot 对齐到期；真实观察/完成时刻不被 slot 替代。失败或 partial 仍不撤回成员；完整列表完成前公开只保留先前事实。内部 metadata 保存 source scope 的 nextDueAt/lastSuccessAt/lastAttemptAt，不生成任何插件质量或推荐分数。私有 state 首版格式1/query 首版格式1 不变。

## 指定来源筛选规范目录

来源浏览页新增“查看关联规范包”，进入 /catalog/?recommendationSource=<source-id>&revision=<revision>&sort=name；名称排序是已有目录规则，不是来源排名。目录的“精选来源关联”选择器分批读取来源选项（每次 20 个），可独立重试和加载更多；已经选中的来源不因稍后加载选项而被覆盖。候选 scope 隐藏并重置该规范目录筛选，旧快照明确提示能力缺失。

后端先按 canonicalLatest 的精确声明链接与 active 来源成员过滤全目录，再执行其他筛选、排序和游标分页。无明确最新规范版本、只有历史版本链接或已撤回的成员不进入当前匹配；同包有多个声明链接只返回一次。结果披露 source/version/canonicalHash、动态 stale 和 unknown 排名，不推断背书或质量，不按来源数量制造排名。客户端拒绝错误来源/版本/hash/状态、虚构排名与未筛选回退。详见 [HTTP-CONTRACT.zh-CN.md](HTTP-CONTRACT.zh-CN.md)。

只读 CLI：

~~~powershell
.\.venv\Scripts\python.exe -m agent_forge_service.worker query --runtime .\query-runtime --recommendation-source "curated-source:<64位hash>" --sort name --limit 20
~~~
