# 公开发现候选：投影与查询契约

当前服务格式统一为首版1；版本规则见[首版格式决策](../docs/decisions/0002-initial-service-formats.zh-CN.md)。

日期：2026-10-12。候选采集、SQLite 公开投影、QueryService、CLI、HTTP 契约和公开 bundle 接收链路已接线。本文描述本地实现；前端候选界面已用真实 Edge 和 SQLite 响应夹具验收，真实 FastAPI 监听与生产容量仍须单独验收。

## 目录与候选的边界

默认 `scope=catalog` 只返回已收录的 canonical 包。显式 `scope=discovery` 返回独立候选 DTO；候选没有 `canonicalRecord`、安装承诺、兼容性认证或隐含推荐分。

| 候选 | 身份与事实 | 不推断的内容 |
| --- | --- | --- |
| repository | `github-repository:<GitHub 数字 ID>`；仓库名、公开 Topics/计数/时间、搜索页 hash/URL/观察时间 | Topic 不决定包类型、Agent 兼容性、版本或质量 |
| manifest | `manifest:<固定声明身份的 SHA-256>`；仓库 ID、明确路径/格式/Agent 关联、固定 commit/blob/hash、声明字段、issues、生命周期 | Schema 合格仍是 `review_pending`，不自动采纳，也不证明 Agent 兼容性 |

manifest 声明字段仅含 name/description/version/license；缺失保持 null，缺失项和校验问题另列。Skill 正文、命令字段、任意未选取字段不公开。用于检索的名称最多 4096 字符、描述摘要最多 400 字符，并清理控制字符；完整选取的声明字符串保留在 declaredMetadata，不能将展示截断值当作实际发行版本。单项 DTO 上限 256 KiB，HTTP 响应继续受统一 2 MiB 上限约束。

来源证据按公开 topic/timeField 身份去重，不按本地 credential profile 分组。每个仓库展示最多 20 个来源证据，sourceCount 保留总数，evidenceTruncated 明确标记省略。证据包括结构化查询窗口/页码、incomplete、总数/页内数、hash、URL、observedAt 与 checkedAt；不公开 gh 标签、账号/配额、任务 ID、租约、Worker 配置或请求头。

当同一 revision 已有可证明的 canonical 仓库映射，仓库候选退出 discovery；manifest 只有精确声明身份已收录时退出。匹配不依赖相同名称或相似描述。同仓库的其他未收录声明仍可作为 manifest 候选存在；私有证据历史保留，旧 revision 的候选不被改写。

## 截止时间与发布

搜索候选的私有摘要会随重命名或后续观察变化。公开投影重新读取 checkedAt/observedAt 均不晚于 generatedAt 的保存搜索页，验证 body 的 SHA-256，并按对应 page hit 选择仓库。因此后续改名、Star 增长、新候选和未来完成状态不能回填旧快照。

manifest 每个仓库/路径/格式/Agent/commit 对应不可变候选；只发布截止前创建的候选和截止前的生命周期事件。当前 mutable state 字段不代替历史事件。Agent 关联来自明确声明检查 profile，仍标记 `explicit-profile-not-compatibility-verification`。

新快照沿用 query schema 首版格式1，增加 `features.discovery=true` 和 discoveryCoverage；私有 state 仍为 首版格式1，无新迁移。正式包/版本 counts、Dashboard 和目录榜单继续只统计 canonical。候选有独立总数、来源数和时间范围。

bundle 校验检查声明/表/列、固定 SQL 检索模型、公开 DTO 的字段与日期、身份/hash/URL、Agent 关联、source status 与覆盖统计。未知私有字段、未声明表、未来观察、伪造 profile、改变 tokenizer 或重新计算 hash 后的语义冲突会拒绝。校验和用于一致性，可信 expected bundle hash 仍需通过可信交付渠道取得；不将本地内部一致性校验描述为重新在线验证全部上游。

## 查询

| 操作 | 契约 |
| --- | --- |
| GET /api/v1/packages?scope=discovery | q/agent/type/subtype/facets/customFacets/limit、sort、revision/cursor；独立 items/coverage/freshnessSummary |
| GET /api/v1/discoveries/{id} | 指定 revision 的候选详情；未找到为 discovery_not_found/404，格式错误为 invalid_query/400 |
| GET /api/v1/sources | 规范来源声明以及公开 GitHub 搜索来源状态；不表示其他 Registry/Release/指标来源的治理投影均已完成 |

候选默认 sort=first_seen，按首次**发现**时间降序和 ID 升序排列；首次发现不是上游创建时间。还支持 name 和有搜索词时的 relevance。仓库公开计数仅作为附带原始事实，此 scope 不接受 stars/downloads/增长榜排序；不把尚未具备完整窗口/来源映射的候选混入正式榜单。仓库候选 type 为 null、agents 为空；按 Agent/type 筛选只会命中有明确声明的 manifest。

搜索复用 unicode61、可用时的 trigram 和短中文名称/标签路径。没有支持所需 trigram 的快照明确返回 search_unavailable，不扩大为全描述扫描。当前声明没有 facet，相关筛选不会返回虚构匹配。

游标绑定 scope、revision、全部规范化筛选、排序、页大小和 policy。新 current 不改变旧页；改变条件返回 cursor_mismatch，旧 revision 被回收返回 cursor_expired。未声明 discovery 的旧快照可以继续查询正式目录，候选列表/详情返回 discovery_unavailable/503。推荐接口继续 recommendations_unavailable/503，来源收录不能冒充推荐事实。

每项返回动态 freshness。候选集合以最旧的 lastObservationAt 判断过期，避免新鲜头部掩盖长尾；coverage 仍为 not_guaranteed，不承诺全网完整。当前提示阈值是 24 小时，不是调度间隔或完整覆盖 SLA。

GitHub 搜索来源状态按 topic/timeField 聚合：complete/partial/pending、lastSuccessAt、lastObservationAt 与动态 stale。只有 cutoff 前实际完成的查询窗口可以报告成功；完整空结果也是成功。complete 仅表示该次已保存查询窗口完成，不表示一个 Topic 的所有历史或全 GitHub 覆盖。来源状态不包含私有 run/shard ID。

CLI 可以在只有公开快照的接收端执行：

~~~powershell
.\.venv\Scripts\python.exe -m agent_forge_service.worker query --runtime .\public-runtime --scope discovery --revision <revision> --limit 20
.\.venv\Scripts\python.exe -m agent_forge_service.worker detail --runtime .\public-runtime --scope discovery --revision <revision> github-repository:123
~~~

query-client.js 和 /catalog/ 已接入候选列表、独立详情、范围切换、筛选元数据和 revision 校验。候选仅支持最近发现、名称和有关键词的相关性排序；首次发现不等于上游创建。GET /api/v1/facets?scope=discovery&revision=... 只返回候选明确声明关联的 Agent 和 type，不借用正式目录统计。Topic 不推断 Agent/type，声明检查 profile 也不证明兼容性。

候选详情展示缺失声明、issues、生命周期、固定 commit/path/hash 和公开来源；仍需补元数据或审核，不出现安装入口、规范版本选择或推荐。筛选元数据失败有独立重试；查询失败保留已显示数据及其范围标记。深链接为 #/discovery/{编码后的完整 id}?revision=...；正式目录仍使用 #/id/。

真实 Edge 已检查两版 SQLite 夹具的分页、详情、错误恢复、请求取消、英文、深色和 390px 手机布局；确认 escaped 声明不能生成 DOM 图片，旧游标保持原 revision，回收后明确提示刷新。夹具服务器只回放公开 HttpReadService JSON，不能替代真实 FastAPI/CORS 验收。

这些查询不创建或打开私有 state，不采集、不修改来源、不执行插件。对应参数已经接入 HTTP 契约；尚未安装的 FastAPI 依赖及真实 URL 解码/CORS/浏览器联合请求仍是后续验收边界。

## 验证入口

~~~powershell
.\.venv\Scripts\python.exe -m unittest service_tests.test_discovery_projection -v
.\.venv\Scripts\python.exe -m unittest discover -s service_tests -q
~~~

## 元数据分拣（2026-10-13）

候选的 reviewStatus/issues/lifecycle 继续描述自动分类与声明检查；本地运行者的补充元数据/拒绝收录/不支持/重新待审另存 metadataReview，不批准 canonical 或安全/质量/兼容。状态绑定元数据视图指纹和上一事件，变化后 superseded，计数更新不改变指纹。默认仍展示全部候选；scope=discovery 可选 reviewDisposition，旧 feature 缺失明确503。

本地 CLI candidate-review-inspect/record、公开 ledger 重放、当前格式的私有备份/恢复与候选 UI 已接线。公网上没有写入接口，私有 operator label/request key 不公开；进入 canonical 和候选导出仍需既有授权流程。完整规则、命令、迁移与测试边界见 [CANDIDATE-REVIEWS.zh-CN.md](CANDIDATE-REVIEWS.zh-CN.md)。
