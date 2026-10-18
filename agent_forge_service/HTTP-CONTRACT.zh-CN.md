# 公共 HTTP 查询契约与验收边界

当前服务格式统一为首版1；版本规则见[首版格式决策](../docs/decisions/0002-initial-service-formats.zh-CN.md)。

## 路由与参数

只支持 GET；未知参数、重复参数、空的结构化参数和未知排序明确拒绝。所有错误是 JSON：`{"error":{"code":"invalid_query","message":"Invalid query parameters."},"requestId":"..."}`，另有一致的 X-Request-ID。错误响应 no-store，不含 SQLite 异常、磁盘路径或 gh 凭据。

| 路由 | 参数与返回 |
| --- | --- |
| /health/live | 无参数，返回进程状态；不读取目录或私有 state |
| /health/ready | 无参数，有校验通过的当前公开快照则就绪；不要求采集来源此刻成功 |
| /api/v1/packages | q、agent、type、subtype、scope、sort、limit、facets、customFacets、metricSource、recommendationSource、reviewDisposition、cursor、revision |
| /api/v1/packages/{id}/detail | revision、version；返回嵌套 canonicalRecord、原始观察和 canonicalLatest。指定 version 必须精确命中；缺失返回 version_not_found/404 |
| /api/v1/packages/{id} | 详情简写；完整 ID 与子路由后缀有歧义时使用 /detail |
| /api/v1/packages/{id}/versions | revision、limit、cursor；所有规范版本分页，不以字符串排序猜 latest |
| /api/v1/packages/{id}/metrics | revision、limit、cursor、source；source 为 github.com 或 npmjs.org，默认 github.com。缺少指标返回 unknown 与空列表，不能补零 |
| /api/v1/changes | revision、since、limit、cursor；按事件 seq 增量分页 |
| /api/v1/facets | revision、scope（默认 catalog）；catalog 返回规范 facet/customFacet 计数与 agents/subtypes；discovery 返回独立候选 agents/types，facets/customFacets/subtypes 为空，不推断 Topic。旧版无 discovery 为 503；未知 scope 明确拒绝；尚未包含完整管理员 vocabulary 治理 |
| /api/v1/sources | revision；规范来源声明与公开 GitHub 搜索窗口状态，含 lastSuccessAt/lastObservationAt/partial/stale；其他来源治理仍待接入 |
| /api/v1/manifest | revision；校验后的 manifest 和动态新鲜度 |
| /api/v1/dashboard | revision、可选 agent；同快照统计，未知 Agent 明确 404 |
| /api/v1/recommendation-sources | revision、limit、cursor；来源目录和配置范围/动态 stale，不含私有正文 |
| /api/v1/recommendations | 必需 source；可选 status=all/active/withdrawn、revision、limit、cursor；按 source-target 字母序浏览来源成员 |
| /api/v1/packages/{id}/recommendations | revision、limit、cursor、source、status、version；精确规范版本声明链接关联，缺失版本 404；旧快照缺少 recommendations feature 为 503 |
| /api/v1/discoveries/{id} | revision；独立候选 DTO 和公开证据；未知候选 discovery_not_found/404；没有投影的旧快照 discovery_unavailable/503 |

ID 允许 slash 和 Unicode。前端对原始 ID 执行一次 encodeURIComponent，并为详情追加 /detail；例如 ID dsh/plugin/example/metrics 的详情路径为 /api/v1/packages/dsh%2Fplugin%2Fexample%2Fmetrics/detail。FastAPI 子路由采用 path converter，先注册固定后缀，避免普通单段 ID 路由截断。客户端必须对原始ID只编码一次，代理应保持编码后的路径语义。

limit 默认 20、最大 100（受 Settings 限制）。since 是 0 至 SQLite 有符号 64 位整数上限。facets/customFacets 是 JSON 对象；重复 JSON key、非有限数字、未知种类和超量取值拒绝。QueryService 统一做词数、类型、facet 和查询期限检查。

catalog 排序当前支持 canonical_updated、first_seen、name、带 q 的 relevance，以及明确来源的 stars、star_growth、repo_pushed、downloads、download_growth，以及显式 releaseSort feature 的 latest_release。latest_release 仅使用当前规范版本明确分发关联的发行时间，包含预发行、排除未来时间、未知最后；策略/覆盖和依据见 releaseSortPolicyVersion/releaseSortCoverage/latestRelease。旧快照返回 sort_unavailable，保留已支持的发行列表查询。下载两榜必须指定 metricSource=npmjs.org。没有使用人数事实，因此不接受 most_used。每种榜单继续披露来源、实际区间、缺失和资格；缓存不改变原始事实。

显式 scope=discovery 使用独立 DTO，默认 first_seen；支持 name/带 q 的 relevance，不接受正式目录的指标排序。查询筛选/游标/revision 绑定同一公开快照。候选 type/Agent 仅由明确 manifest 声明检查 profile 关联，Topic 命中不推断兼容性；候选不冒充可安装 canonical 包。详细证据与旧快照契约见 [DISCOVERY.zh-CN.md](DISCOVERY.zh-CN.md)。

## revision、游标与缓存

每个请求先固定公开 revision，查询 SQL、响应元数据和缓存键共同使用它。当前指针切换只影响之后的新请求。cursor 继续绑定原 revision、完整筛选、排序、页大小和策略；显式 revision 与 cursor 不一致返回 cursor_mismatch/400。旧快照已回收返回 cursor_expired/410，缺失的显式 revision 返回 revision_unavailable/410，不续查新 revision。

查询核心与 HTTP 共用完整版本和单一不可变快照，不增加数据库构建链、不在访问请求中采集、不打开私有 state。QueryService 以 mode=ro&immutable=1 查询；每个池中实例只给一个已准入请求使用。校验缓存有限保留，当前指针的 manifest 校验每请求核对；已回收文件即使有响应缓存也不会继续返回。

首期响应缓存为进程内 LRU：最多 128 条、8 MiB，单响应最多 2 MiB。ETag 绑定契约、端点、ID、revision、规范化完整 query、manifest 和响应字节；支持 If-None-Match，包括 weak validator 和多个标签。新的 requestId 每请求生成，不保存在共享缓存正文中。

snapshotAgeSeconds 和新鲜度会随时间变化，不能只用 revision 永久返回 304。目前采用最多 5 秒的短 TTL，到期重新计算 DTO 和 ETag。Cache-Control 的 max-age 是此缓存条目的剩余时间，304 不重置有效期；新鲜度越过阈值可能有最多 5 秒的缓存滞后。当前阶段所有查询均使用短缓存，没有声称已实现不带动态新鲜度的长期不可变响应。

## 资源边界与适配层

默认最多 4 个在途查询；超额立即 service_busy/503，不在数据库连接队列等待。全实例 token bucket 默认每秒 20、突发 80；超额 rate_limited/429。live 不消费查询额度，ready 使用同一查询准入。没有按客户端身份分配额度，也没有共享多进程限流。

解码后查询参数总量最多 16 KiB；适配层另检查原始 query string 16 KiB。动态 ID 最多 4096 字符，路径最多 16 KiB。SQL 使用现有 progress deadline，默认 2 秒。响应限制在 DTO 序列化后检查；初次快照校验与序列化不包含在 SQL deadline 内，不能据此声称所有请求墙钟时间或内存都严格限制为同一个值。read API 并发及字节缓存限制已验证，公网容量仍待实测。

app.py 默认关闭 docs/OpenAPI，仅显式配置精确 CORS origin；默认同源，不允许 credentials 或任意 origin。CLI 默认绑定 127.0.0.1，单 Uvicorn worker，关闭自动信任代理请求头，设置入口并发和 backlog。代理自身产生的超载/网络错误不承诺都符合应用 JSON 契约。上线仍需 TLS、请求/连接限制、只读挂载、文件权限、重启/切换/回滚和负载验证。

## 预备启动与现有可运行验证

2026-10-14 已将 HTTP 运行依赖固定到可选 extra `http`，将 HTTPX 固定到独立 `http-test` group，并更新 uv.lock / 带 hash 的生产 requirements.txt；原有基础依赖版本保持。2026-10-14 获准后已安装；2026-10-15 六项 ASGI/TCP 通过。安装、只读预检、systemd/nginx 样例见 [部署准备](../deploy/query-service/README.zh-CN.md)。本机实际测试采用的启动入口为：

~~~powershell
.\.venv\Scripts\python.exe -m agent_forge_service.app --runtime .\snapshots\query-service --host 127.0.0.1 --port 8791
# 跨域前端按实际地址逐个添加 --allow-origin https://example.test
~~~

目前可运行：

~~~powershell
.\.venv\Scripts\python.exe -m unittest service_tests.test_http_contract -q
.\.venv\Scripts\python.exe -m unittest discover -s service_tests -q
~~~

测试覆盖精确旧版本与缺失、slash/Unicode/后缀 ID、分页、revision 切换竞态、游标回收、规范化缓存、条件请求/剩余 TTL、新鲜度阈值、速率/并发/响应上限、错误脱敏、readiness、私有库无变化及禁止网络调用。FastAPI 启动、真实 URL 解码、HTTP CORS/异常中间件、浏览器交互、TLS/代理/重启、生产并发和前端接入仍是独立验收门。

## Dashboard 统计契约（2026-10-12）

GET /api/v1/dashboard 接受 revision 和可选 agent；请求先固定 revision，随后读取同一快照内的公开 dashboard_summaries。未知 Agent 明确 agent_not_found/404，缺失 revision 仍为 revision_unavailable/410，禁止回退到当前 revision。Agent 参数、缓存键、ETag 与查询准入沿用统一契约。FastAPI 路由已接线，其真实启动/CORS 仍待验证。

返回 scope、totals、types、agents、facets/customFacets、compatibility、recentPackages、policyVersion、countingPolicy 和 projectionMode。全局按规范 ID 去重；各 Agent 按目录声明关联计数，可以重叠。版本数量先按此快照的 Agent 目录关联筛选包，再计入这些包的全部规范版本，不是每个历史版本分别声明该 Agent 的数量。Bundle 只计独立 Bundle 记录，不宣称已展开嵌套成员。

facet 两栏分别按选中的规范版本声明聚合，每栏最多 20 项，计数降序、kind/value 并列稳定排序；自定义标签不能作为规范认证。兼容性单位为 package-agent-declaration：规范版本未知、没有范围或存在未知声明均归 unknown，不表示不兼容。最近 10 个包仅按 canonicalUpdatedAt 降序、ID 并列排序，不冒充上游创建/发行时间。

新快照 features.dashboard=true，摘要与 FTS/查询表在同一发布事务中预计算。摘要使用独立公开表，保留 metadata 与 manifest 的严格一致性；单摘要上限 2 MiB。公开 bundle 导入/导出校验表结构、精确范围、字节上限，并在原有验证期限内重新聚合、逐字比较规范摘要，拒绝重新计算 hash 后的错误数量。旧快照不含该 feature 时，在只读连接和 Python/SQL 期限内按需聚合，projectionMode=on-demand，不读取私有 state。新快照为 precomputed；动态新鲜度仍使用至多 5 秒的响应缓存。

## 来源精选查询的边界

公开 policy public-curated-projection-v1；sourceRank=null，字母序不是推荐顺序。来源目录与成员、当前/前一完整 snapshot 的 commit/blob/content hash、截止时间和引用均来自同一 revision。游标绑定查询路由、package/source/status/version/limit/policy/revision；修改条件返回明确错误。未知 source 为 recommendation_source_not_found/404，未知 version 为 version_not_found/404。包关联只能来自该版本 canonical JSON 的完全相同链接目标，仓库根不覆盖任意子插件；association=exact-declared-link、endorsement=not-inferred，不推断包质量、安全或使用人数。

空 items 返回 availability=unknown；新鲜度采用实际成功/观察时间，超过 86400s 标 stale 而不自动撤回。每成员引用最多 20 条，citationCount/citationsTruncated 披露截断；withdrawn 保留移除前与移除后完整证据。来源别名只合并 label，不能多算独立来源。所有路由继续仅 GET、参数白名单、ETag/缓存/限流与脱敏错误。前端传输与解码后的 HTTP 契约已验证，推荐 UI 和实际 FastAPI 编码/CORS 验收继续未完成。详细规则与命令见 [RECOMMENDATIONS.zh-CN.md](RECOMMENDATIONS.zh-CN.md)。

## 完整规范目录的指定来源筛选

/api/v1/packages 可选 recommendationSource=curated-source:<64位小写hex>，仅支持 catalog scope。筛选在 SQLite 完整数据上先执行：curated_package_links 的 package_id/version 必须等于 p.id/canonical_latest，同 source/target 的当前 claim 必须 active，再叠加 Agent/type/subtype/facets/q，最后按已有显式 sort 排序并取页。EXISTS 避免同包多个匹配链接或多个 Agent 关联制造重复行；仓库根不能覆盖未被明确选择的子路径。没有 canonicalLatest、只有旧版本关联或已 withdrawn 的包不进入这个当前目录筛选，历史事实仍可通过精确版本 recommendations 接口查看。

catalog DTO 新增 recommendationFilteringAvailable（此快照是否有能力）与 recommendationFilter（无筛选为 null）。后者披露 sourceId/labels/status=active、canonicalVersionPolicy=canonical-latest-only、association=exact-declared-link、endorsement=not-inferred、sourceRank=null、policyVersion=canonical-latest-active-source-v1 和动态 source freshness。每行 recommendationAssociation 保留 sourceId、规范 version、canonicalHash 与相同关联/状态/未知排名边界。不能把来源成员或共享仓库上下文说成插件背书、质量或安全。

请求与 cursor/cache 绑定 recommendationSource 和 recommendationFilterPolicy，既有 sortPolicyVersion 仍表达当前明确排序，不改变为推荐评分。支持已有 name/canonical_updated/first_seen/relevance/stars/star_growth/repo_pushed/downloads/download_growth，relevance 仍需 q，下载两榜仍需 metricSource=npmjs.org。当前 provider 未提供原始排名，source_rank 不可用，不用行号、来源数、名称或 Star 补造推荐顺序。

无效 ID、空值、重复参数与 discovery scope 下的来源过滤为 400；此 revision 缺少来源为 recommendation_source_not_found/404，旧 feature 缺失为 recommendations_unavailable/503，不退回未筛选结果。来源检查过期继续展示最后完整事实和 stale，不自动撤回；过滤空结果只表示该快照当前规范版本没有匹配，不能断言全网没有推荐。详情、选项、结果和游标必须各自保持同 revision。

## 候选元数据分拣查询

scope=discovery 的 /api/v1/packages 可选 reviewDisposition=not_recorded/rejected/unsupported/needs_metadata/pending/superseded；catalog scope、未知/空/重复值为400。筛选绑定独立 review-policy 和原 revision/limit/sort/cursor/cache，作用于完整候选集合。旧无 discoveryReviews feature 返回 reviews_unavailable/503，不把缺少能力当作没有记录。默认无过滤保留全部候选。

有该 feature 的候选 DTO 增加 metadataReview，独立于自动 reviewStatus/issues/lifecycle；列表/详情包含 discoveryReviewCoverage。每项披露 local-worker-metadata-triage authority、当前指纹、状态、最后事件、最近20条历史、historyCount/historyTruncated。完整事件/上下文由公开接收端校验并重放；只有 cutoff 前的事件可见。字段/规则/顺序/父事件/指纹或计数不一致拒绝，即使重新计算 bundle checksum；不认证操作者身份，也不代表安全或 canonical 收录批准。

operator-label/request-key/任务均不公开；GET/ETag/准入/脱敏仍有效，HTTP 写入继续405。写接口只在本地 Worker CLI，规则与迁移见 [CANDIDATE-REVIEWS.zh-CN.md](CANDIDATE-REVIEWS.zh-CN.md)。

## 本地来源采集控制（2026-10-14）

来源采集控制已接入 JobQueue/批次资格、单写入者 CLI、独立公共完整账本与 GET 分页，以及前端独立折叠面板。支持提供方、仓库 ID/显式名称、Topic、npm 包和精选 Markdown 文件范围；任意匹配停用都阻止新任务领取。停用/恢复不删除事实、不改变 Star/下载或来源成员撤回、不推断质量/安全/兼容性，也不自动采纳规范候选。私有 state 升 首版格式1，query 保持 首版格式1；截止前无控制事件继续使用旧表/DTO，明确查询返回 source_controls_unavailable/503。

公开接口为 GET /api/v1/source-controls，允许 revision/limit/cursor；写入只通过本地 source-control-inspect/source-control-record，公开服务不打开私有 state。CLI 的 expected-head/CAS、幂等请求键、固定原因与全局单调时钟、已开始请求的边界、名称解析不确定性，以及独立面板的版本显示详见 [来源采集控制说明](SOURCE-CONTROLS.zh-CN.md)。没有实际部署或对真实来源作停用决定。

## 公开Release与资产观察（2026-10-14）

截止前成功Release页面现可随query 首版格式1发布独立可重放投影，feature按实际证据出现。GET /api/v1/releases 与 /api/v1/packages/<id>/releases 支持固定revision分页、指定规范版本、精确声明/仓库上下文筛选；新增只读release-query CLI与详情独立折叠面板。累计资产下载请求单列，不冒充人数/npm或插件版本；未知/缺页/partial/旧tag重用保持不确定性。接收端从原页与规范URL重新计算关联，未安装新依赖，不自动下载、采纳或认证安全/兼容性。规范目录latest_release排序尚未接入。完整字段、边界与验证见 [Release观察说明](RELEASE-OBSERVATIONS.zh-CN.md)。
