# 公开 Release 与资产观察

当前服务格式统一为首版1；版本规则见[首版格式决策](../docs/decisions/0002-initial-service-formats.zh-CN.md)。

日期：2026-10-14，Asia/Shanghai。已采集的仓库级 Release 元数据可随不可变查询快照公开，并根据规范记录的显式URL建立可重建关联。首期SQLite/单Worker不变；私有state 首版格式1与query schema 首版格式1保持。没有自动采纳、安装、下载插件文件或把仓库tag猜成插件版本。

## 保存和公开的事实

私有采集仍使用匿名公开Release GET、受限页数与断点。公开投影增加5张条件表：public_release_pages、public_release_repositories、public_release_items、public_release_checks、public_release_package_links。只有发布截止前存在成功并已提交的页面才发布features.releases=true和releaseCoverage；无页保持原DTO/表，明确查询返回releases_unavailable/503。失败/404/限流不当空页，也不创建“没有发行”的结论。

页面保留白名单Release/asset字段、原规范化响应hash、公开仓库ID、页码、选择/观察/检查时间与响应200/304。scan/page公共身份从公开字段计算，不包含私有job、lease、credential identity或操作者。源正文、作者、下载地址、认证信息不进入该Release投影。repo名称proof来自截止前最新成功仓库公开观察，接收端核对原白名单响应/hash与归一化身份；没有proof仍可按稳定ID浏览，不能据此猜包关联。页与名称proof均有4MiB边界。

public_release_items按(repositoryId,releaseId)保存截止前最新已观测内容，选择策略为observedAt、checkedAt、扫描slot、页码、公共pageId确定性排序；完整原页面保留用于重放。304沿用原observedAt与计数，仅推进checkedAt，不能制造新计数采样。新短页/空页不删除旧发行或资产身份，跨页重复/页预算耗尽为partial；短页结束只称traversal_complete，不能证明一致快照或删除。未终止页链为in_progress。

ID、asset sizeBytes与downloadCount以十进制字符串返回，覆盖原64位整数范围，避免JavaScript Number丢精度。资产计数的unit=download_requests、measurementKind=cumulative、scope=github_release_asset；counterIdentity包含仓库/发行/资产ID。计数0不转unknown，下降/重置仍保留原值；目前不计算资产增长率，不跨资产/发行/仓库/npm相加，不称使用人数。资产重传/改名需依原ID和声明重新判断。

## 包版本关联规则

对每一个已公开规范版本重新读取canonical_json、核对package ID/version/payload hash，然后按公开repo名称proof建立关联；名称映射冲突时不猜。名称只在该快照证明，不追随未知改名、陌生别名或重定向。

| 规范声明 | 关联 | 不代表 |
| --- | --- | --- |
| links.repository 或 git/github-repo 分发 | repository_context，所属仓库发行 | 该发行属于此插件/历史版本 |
| release/archive/github-mcpb 的明确 GitHub releases/tag/<tag> URL | exact_declared_release | tag等于规范version、代码/兼容/安装已验证 |
| 上述分发类型的 releases/download/<tag>/<asset> URL | exact_declared_asset，匹配确切tag与唯一资产名/ID | 文件已下载、checksum已核实、下载请求等于人数 |

精确tag大小写敏感，不去掉v、不从包名或版本字符串猜。一个tag观测到多个Release ID时不建立精确关联；同一发行的资产名不唯一时不建立资产关联。/releases/latest、任意Query/fragment/credentials、未知主机与含不确定路径的下载链接不用于精确匹配。其他Registry/动态URL保持unknown，不静默改成仓库背书。

规范version=2.0.0可以明确声明tag=v1.0.0；页面展示“规范声明版本2.0.0”和“上游tag v1.0.0”，关联只是公开声明，不证实内容对应。历史规范版本按自己的分发URL计算，不借用最新版本声明。新的资产改名会使旧精确资产链接不再匹配当前已观测内容，但不会把这解释为上游已删除。

## 查询与分页

~~~text
GET /api/v1/releases?repositoryId=123&revision=<revision>&limit=20
GET /api/v1/packages/<encoded-id>/releases?version=1.0.0&revision=<revision>&association=all&limit=20
~~~

全局接口可省repositoryId浏览已观测发行，包接口可省version读取规范latest；显式缺失版本404，不回退到其他版本。association=all/declared/repository_context，其中repository_context表示仅上下文（有精确声明的同项不纳入此筛选）；全局接口不接受版本或关联过滤。路径中的ID应整体编码，包ID以/releases结尾时仍可通过明确/detail与末尾/releases端点访问。

顺序固定为上游publishedAt降序，再仓库ID/发行ID升序；这是观测发行的稳定查询顺序，不是插件质量/使用排序，也不改变规范目录的默认排序。cursor绑定revision、包/版本、页大小、repositoryId、association与policy；更新发布不会移动旧页，过期410且不自动改用新revision。

每项返回release、association、freshness，响应披露releaseCoverage和最多20个repositoryChecks/完整范围数量/截断。新鲜度按实际checkedAt计算，不能把不完整遍历或旧计数当完整覆盖。无匹配返回availability=unknown；原始空页可以显示“遍历结束、观测身份0”，仍不宣称上游没有发行。hash/checksum用于完整性，不认证运行者、远端账号或源码可信度。

~~~powershell
.\.venv\Scripts\python.exe -m agent_forge_service.worker release-query --runtime .\snapshots\query-service --revision "替换为公开版本" --repository-id 123 --limit 20
.\.venv\Scripts\python.exe -m agent_forge_service.worker release-query --runtime .\snapshots\query-service --revision "替换为公开版本" --id "替换为包ID" --version 1.0.0 --association declared --limit 20
~~~

查询/公开bundle接收端不需要canonical checkout或私有state。接收端从原始公开页重建遍历状态、最新观测项、计数与包关联，核对精确SQL/字段、页链/截止/hash/覆盖/规范版本声明；修改派生计数、关联、页链、schema或隐藏feature后重新计算外层checksum仍会被拒绝。外层checksum须来自可信交付渠道，不能将完整替换原证据后的自签checksum当远端来源认证。老接收端未认识新表时会拒绝导入，需要使用支持该feature的接收端。

## 规范目录的最新声明发行排序（2026-10-14）

`GET /api/v1/packages?sort=latest_release` 与 CLI `query --sort latest_release` 在完整规范目录上过滤、排序、keyset 分页。只有带 `features.releaseSort=true` 的快照支持；该能力另要求已验证的 Release 原页投影。旧快照包括已经有 `features.releases` 的旧 bundle 继续可读取发行列表，但新排序明确 `sort_unavailable`，不回退其他排序。

策略 `canonical-declared-release-time-v1`：

- 只使用 `packages.canonical_latest` 对应规范记录的明确分发 URL，以及已投影的 `exact_declared_release` / `exact_declared_asset` 关联。历史版本的关联、仓库主页、git 仓库和仅 `repository_context` 的记录不能贡献时间。
- 在关联发行中取最大的 `published_at`；包含明确声明的预发行并保留 `prerelease`。并列时选择数值最小的 repository ID、release ID。不会移除 tag 的 v、猜测 canonical version 与 tag 一致，或挑选未经声明的“最新稳定版”。
- 晚于快照 `generatedAt` 的源发行时间仍保留在原始发行观察中，但不参与排序，记录 `futureCount`。如果全部关联时间都在未来，返回 unknown / `future_published_time`。
- 包之间按有效发行时间降序、稳定包 ID 升序；unknown 在最后，并按包 ID 升序。没有关联保持 unknown，不拿仓库推送、检查、规范更新时间补齐。

公开派生表 `public_release_sort_keys` 为每个规范包保存一行；`public_release_package_rank(published_at DESC,package_id ASC)` 服务完整目录排序，无搜索时从该索引开始查询。新表不更改私有 schema 或公共查询首版格式1。manifest 添加 `releaseSortCoverage`、`releaseSortPolicyVersion` 和 availableSorts 中的 `latest_release`。bundle 接收端用原发行页及精确规范关联重新推导全部排序值，检查 SQL/index、完整行集、规范 JSON、覆盖与策略；只重算外部文件 hash 不能伪造索引。

列表与详情的 `latestRelease` 都明确指向 `canonicalVersion` / `canonicalHash`，包含 known/unknown、原因、关联/有效/未来项数及选中发行的原始证据字段。读取历史版本详情时，这个字段仍描述 canonicalLatest，不能作为历史版本自己的发布时间；历史发行证据通过已有 `/releases?version=...` 查询。`observedLatest` 继续为 unknown。

该时间表达“当前规范版本明确声明关联的发行中，最新一次观察到的发布时间”，不能证明所有上游发行已收集、插件已安装、人数、质量、安全或兼容性。排序以不可变快照为准；快照和原始观察时间均随响应披露，未更新的证据不会因新检查被改成新发行。

复验：`python -m unittest service_tests.test_release_sort`。前端完整排序/20+8分页、搜索和 Agent/type 筛选、旧 revision/过期游标/请求取消与英文移动端由 `tests/release-sort-browser.test.cjs` 使用真实 Edge 和公开 SQLite 接收端响应验证。它仍不替代实际 FastAPI/CORS 或生产负载。
