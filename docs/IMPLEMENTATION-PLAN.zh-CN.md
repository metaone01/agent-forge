# Agent Forge 实施计划

状态：待审核草案

本文是实施计划，不是开始实施的授权。需要在修改当前 Schema、源目录和仓库职责之前完成审核。

## 1. 已确定的设计决策

- Agent Forge 是元数据整合服务，不检查、执行、扫描、认证或验证上游内容。
- 任何字段都不代表安全、可信、准确、完整、及时、兼容或可用保证。
- 四种基础类型为 mcp、plugin、skill、general。
- general 替代当前的 other。它表示不属于前三种基础类型、但确实用于 Agent 工作流的工具，必须声明 `toolType` 和 `agentUse`。
- bundle 是第五种发布类型，用于组合已有包，不代表一种上游工具类别。
- 首期 GitHub-native 模式下，Git 仓库中的规范 JSON/YAML 文件是唯一可写的数据事实来源；后续后端模式再迁移到规范数据库。
- GitHub Pages 只负责展示和提交入口。规范文件通过 Pull Request 修改，Agent 文件、JSON 索引和 manifest 由 Actions 生成。
- Bundle override 只作用于一个 Bundle，不修改源包本身声明的兼容范围。
- 静态 JSON 仍然是对消费者开放的只读契约。

### Schema v2 命名和两层镜像模型

- Schema v2 不再使用 `kind`、`memberKind` 或 `versionKind`。
- 一级包分类使用 `type`；Bundle 成员使用 `memberType`；版本规则使用 `versionScheme`。
- General 工具的具体性质使用 `toolType`，不使用容易与包分类混淆的 `typeRef.type`。
- 插件或包本体的可安装候选使用 `distributions[]`。其中 `distributions[].type` 表示 `git`、`release`、`archive`、`registry` 等发行方式；它不是 Agent Forge 元数据源。
- Agent Forge 元数据源镜像单独由 source manifest/index 的 `mirrorOf`、`sourceMirrors`、`revision`、`priority` 等字段描述。元数据镜像只复制索引和详情，不等于插件本体镜像。
- 同一个包可以有多个 `distributions`，安装器按自己的网络、地区、格式和策略选择候选。Agent Forge 只整合候选地址，不下载、代理、比较或保证候选内容。
- 当多个元数据源返回相同 `packageId`、版本和 `revision` 时，消费者可以切换镜像；revision 不一致时必须报告数据冲突或选择明确优先级，不能静默合并。

## 2. 范围与非目标

### 本期范围

- 用于创建、编辑、搜索、审核和发布元数据的网页与 API。
- Agent 注册和 Agent 版本兼容声明。
- mcp、plugin、skill、general、bundle 五种类型。
- 能力、影响、数据处理、权限、运行环境和集成方式等受控 facet。
- 可选的按 Agent、按类型生成的只读数据库快照；它不是首期在线搜索的必需依赖。
- 静态 JSON 投影。
- Bundle 版本范围交集、成员锁定和 Bundle-local override。
- 审计历史、发布 revision、生成文件 checksum、生成文件签名和回滚。

### 明确不做

- 恶意软件扫描或源代码审查。
- 验证发布者声明、URL、checksum、signature、注册表记录或兼容性声明。
- 执行安装命令或上游脚本。
- 把 facet 当作安全认证。
- 维护二进制文件或充当软件包镜像。
- 允许人工直接修改生成的 Agent 源文件。

## 3. 总体架构

    GitHub Pages 网页 / Issue Forms
        |
        v
    Pull Request <--- GitHub 账号和仓库权限
        |
        v
    GitHub Actions 校验、预览和生成
        |
        v
    Merge Queue -> main -> Pages / Releases

建议在当前仓库的父目录使用以下结构：

    sourcerepo/
    |- agent-forge/              # 公共 Schema 和只读生成数据
    '- agent-forge-platform/     # 网页、API、数据库、Worker、部署

首期不要求服务器、PostgreSQL、FastAPI 或常驻 Worker。GitHub Actions 是校验和生成 Worker，Git 历史、PR、Review、Checks 和 Merge Queue 构成审计轨迹。后端 API、规范数据库和专用 Worker 移到后续阶段。

### 后续后端开发期部署（非首期阻塞项）

如果后续启用 API、数据库和专用 Worker，首轮部署目标是主人提供的本地服务器 `metaone@192.168.100.197`。该服务器只作为后端开发和集成测试环境，不是 GitHub-native 首期的必要依赖。

部署计划：

1. 在服务器上创建独立运行用户、应用目录、虚拟环境和数据目录。
2. 通过环境变量注入数据库连接、GitHub OAuth 配置、GitHub App 凭据和会话密钥；这些值不得提交到 Git。
3. 使用 systemd 管理 API、Worker 和定时任务；使用反向代理提供统一 HTTPS 入口。
4. 本地测试阶段先验证包创建、subtype 选择、custom facet、Bundle 嵌套、权限失效和搜索流程。
5. GitHub OAuth 必须使用明确的回调地址；如果局域网地址无法满足 GitHub OAuth 的 HTTPS 要求，开发阶段使用专用 OAuth App 和 HTTPS 隧道，不能把生产凭据写入代码。
6. 本地部署完成后，在开发机浏览器打开测试地址进行主路径验证；浏览器自动化不可用时保留可重复的手工测试清单。

后端部署位置与 GitHub Pages 分离：Pages 只服务静态网页和公共 JSON，不能承载 API、OAuth callback、PostgreSQL 或 Worker。

### 首期无后端提交流程

首期包提交使用 GitHub 原生流程：

1. GitHub Pages 的“提交包”按钮跳转到预填充的 Issue Form；有仓库写权限的维护者也可以直接创建 Pull Request。
2. Issue Form 收集包 JSON、Agent targets、类型、管理员 subtype、facet、README/LICENSE 链接和提交说明。
3. GitHub Actions 根据 Issue Form 生成提案分支和 Pull Request；提交者不能直接写入 `main`。
4. Pull Request Actions 运行 JSON Schema、跨文件一致性、重复身份、Bundle Agent 交集、嵌套循环、facet/subtype 词汇和 10 分钟 cutoff 校验。
5. 校验失败时，Action 在 PR 中发表评论并阻止合并；本项目不对外部链接或上游内容做网络核验。
6. 必需 Checks、Review 和 Merge Queue 全部通过后才允许合并到 `main`。
7. 合并到 `main` 后，Pages 构建静态网页和 JSON；UTC `00:00`、`08:00`、`16:00` 的 scheduled workflow 生成该时间点的 Release 资产。
8. Release workflow 先生成并校验全部公共投影，再创建 timestamp Release，最后更新 Pages 的 latest manifest。

GitHub-native 方案可以实现包录入、自动校验、审查、合并、审计和静态发布，但不提供真正的动态 API、数据库事务、服务端 OAuth callback 或实时搜索。提交者身份由 GitHub 账号、Issue/PR 作者、Review 和仓库权限提供；Pages 不能安全地保存 GitHub 写入 token。

Actions 实现注意事项：Issue Form Action 创建 PR 时不能依赖普通 `GITHUB_TOKEN` 触发后续校验工作流，因为 GitHub 会抑制由该 token 产生的递归 workflow 触发。应使用受限权限的 GitHub App token，或使用 `workflow_run` 编排生成和校验流程。Merge Queue 相关 workflow 必须监听 `merge_group` 事件，并把同一组必需 Checks 配置到分支保护规则中。

## 4. 类型和 Agent

### 类型注册表

规范类型集合为：

    mcp
    plugin
    skill
    general
    bundle

类型注册表保存在规范数据库，并导出到公共 Schema 元数据中。新增一级类型仍然属于公共契约变更。

### 可选 subtype

五种类型都提供可选的 `subtype` 字段，但 subtype 必须由对应类型的注册表控制，不能把任意字符串直接当成公共分类：

    target.type       = plugin
    target.subtype    = skin

每个类型可以拥有自己的 subtype 词汇。例如：

- plugin：`skin`、`extension`、`adapter`；
- mcp：`local`、`remote`、`gateway`；
- skill：`workflow`、`prompt`、`policy`；
- general：由 Agent 工具实际需要定义；
- bundle：`curated`、`distribution`、`profile`。

这些是初始候选，不代表所有值都必须立即启用。subtype 的规则如下：

1. subtype 为空时，包仍然是合法的一级类型记录。
2. subtype 不改变包的一级类型、Agent 兼容关系或发布数据库归属。
3. 网页和本地数据库可以按 subtype 过滤。
4. 不同 subtype 只有在安装方式、生命周期或权限模型确实不同，才需要增加专门 Schema 约束。
5. subtype 只允许管理员维护，用户不能创建或覆盖 subtype。
6. 包创建时只选择一个 subtype；该选择复制到它生成的每个 Agent/类型投影。

### Agent 注册表

每个 Agent 至少包含：

    id
    displayName
    versionScheme
    versionParser
    sourceSlug
    status

第一阶段优先登记 dsh。版本范围语法不明确或没有对应 parser 的 Agent 仍可收录，但兼容状态为 `unknown`，不能参与自动兼容过滤或 Bundle 范围计算。

## 5. 规范数据模型

首期这些实体以仓库中的规范 JSON/YAML 文件表达，并通过 Pull Request 修改；表格化数据库映射仅用于后续后端阶段。字段语义保持一致，便于以后迁移。

### agents 和 types

保存稳定身份、显示信息、版本规则、生命周期、描述和弃用状态。

### packages

保存包的公共身份和描述信息：

    id, slug, displayName, description, license
    homepage, repository, documentation, keywords
    status, createdAt, updatedAt

包的 id 不因为投影到多个 Agent 而变化。

### 文档和许可证链接

README 和 LICENSE 以链接方式索引，不把长篇正文复制到索引数据库中。包详情和轻量索引可以包含：

    links.readme
    links.license
    links.documentation
    links.repository
    links.homepage

`license` 字段仍保存许可证名称或上游原始声明，`links.license` 保存许可证正文或上游 LICENSE 文件的 URL。`links.readme` 指向包 README，必要时可以同时保存 `readmeSource` 说明它来自仓库、注册表或维护者提供的地址。所有链接都只是元数据整合结果，本项目不下载、审查或验证链接内容。

### package_versions

    id, packageId, version, versionScheme
    releaseNotes, checksumClaims, signatureClaims, publishedAt

checksum 和 signature 是被记录的声明或外部材料。本项目不验证它们。

### package_targets

    id, packageVersionId, agentId, typeId, subtypeId
    agentVersionRange, compatibilityStatus, compatibilityNote
    targetMetadata, installMetadata, status

同一个包版本可以分别声明适用于 pi/plugin 和 hermes/plugin，并使用不同的版本范围和 target 元数据。

### bundles 和 bundle_members

Bundle 有独立的身份和生命周期。成员记录至少包括：

    bundleId, memberType, memberId
    declaredRangeSnapshot
    overrideRange, overrideReason
    overrideActor, overrideCreatedAt

Bundle 只保存成员，不区分必选和可选成员；成员可以是普通包版本或另一个 Bundle。Bundle 创建或修订时保存 declaredRangeSnapshot，避免源包以后修改时悄悄改变已有 Bundle 的语义。循环引用禁止，但嵌套 Bundle 允许。

### bundle_targets

    bundleId, agentId, effectiveRange
    rangeSource, conflictStatus, calculationRevision

### facets 和关系表

使用受控词汇表和规范化关系表，而不是一个无约束的字符串数组：

    facets
    package_facets
    package_effects
    package_data_practices
    package_permissions
    package_integrations

每个声明保留声明来源、可选证据引用和声明时间。证据只保存，不检查。

### releases、projections、sync_jobs、audit_events

记录生成 revision、文件 hash、同步状态、重试、失败原因、操作人、操作内容和回滚点。

## 6. Facet 契约

在包 Schema 中增加带版本的一级对象：

    facetsSchemaVersion: 1
    facets:
      capabilities: [...]
      effects: [...]
      dataPractices: [...]
      permissions: [...]
      runtime: [...]
      integrations: [...]

keywords 继续作为自由文本搜索字段；facets 用于受控语义和稳定过滤。

### 能力 capabilities

第一批受控 ID：

    gui.observe
    gui.interact
    gui.modify
    gui.create
    filesystem.read
    filesystem.write
    filesystem.delete
    code.generate
    code.modify
    code.execute
    browser.navigate
    browser.scrape
    git.read
    git.write
    package.install
    database.query
    database.migrate
    messaging.send
    workflow.schedule
    observability.collect
    domain.pricing

### 影响 effects

表示工具会修改或触发什么：

    workspace.modify
    project.modify
    user-config.modify
    system-config.modify
    remote-resource.modify
    external-message.send
    account.create
    dependency.install
    process.start

### 数据处理 dataPractices

数据类别和动作必须分开。

数据类别：

    public, workspace, source_code, personal, credentials
    financial, purchase_history, telemetry, usage_metrics
    conversation, location

数据动作：

    read, collect, store, process, transmit, share, delete

每条数据处理声明还可以包含：

    purpose
    destination
    retention
    required

例如，domain.pricing 表示工具的业务用途或能力；financial 加上 collect/transmit 才表示它收集或发送金融数据。

### 权限 permissions

第一批权限：

    filesystem.workspace-read
    filesystem.workspace-write
    filesystem.home-read
    network.outbound
    credentials.read
    process.execute
    privileged.required

### 运行环境和集成 integrations

运行环境：

    network.required
    network.optional
    offline.capable
    interactive.required
    daemon.required
    privileged.required
    filesystem.workspace-only
    filesystem.home-access

集成方式：

    agent-ui, ide, browser, terminal, git
    docker, database, cloud-api, mcp-host

### 声明来源

每个 facet 都必须显示为声明，而不是认证：

    publisher-declared
    maintainer-entered
    upstream-documented
    imported

这些来源都不能证明安全或真实性。只要网页显示 facet、verification 字段、checksum、signature 或 advisory，就必须同时显示项目免责声明。

## 7. Bundle 规则

对每个 Agent 分别计算：

    effective(member) = 有 override 时使用 overrideRange，否则使用 declaredRange
    bundleRange(agent) = 所有必选成员 effectiveRange 的交集

规则：

1. 交集为空时，禁止为该 Agent 发布 Bundle。
2. override 只保存在 Bundle 成员关系上。
3. override 必须有原因和操作者。
4. 扩大兼容范围的 override 必须在发布前显示明确警告。
5. 可选成员标记为 conditional，不能悄悄缩小必选成员交集。
6. 禁止 Bundle 循环引用。
7. Bundle 发布时锁定成员版本和 checksum 声明。
8. Bundle facet 由所有成员的 capabilities、effects、dataPractices、permissions、runtime 和 integrations 取并集。
9. Bundle 的可用 Agent 集合是所有成员 Agent 集合的交集；Bundle 不负责安装，安装器自行选择目标 Agent。
10. Bundle 不能隐藏成员已经声明的权限或数据处理行为。

## 8. 本地搜索数据库

按 Agent 和类型生成数据库：

    agents/<agent>/releases/<release>/mcp.db.zst
    agents/<agent>/releases/<release>/plugin.db.zst
    agents/<agent>/releases/<release>/skill.db.zst
    agents/<agent>/releases/<release>/general.db.zst
    agents/<agent>/releases/<release>/bundle.db.zst

四种基础类型加 bundle，因此每个 Agent 有五个数据库。

每个数据库包含：

- 紧凑的包元数据；
- Agent 兼容范围；
- facets；
- FTS5 全文索引；
- 详情 URL；
- checksum 声明。

数据库不包含二进制安装包。

数据库快照不是首期在线搜索的必需路径。只有需要离线或高频本地检索时，才按 Agent/类型生成并发布快照。每个 Agent 的 current.json 将可选数据库固定到同一个 release revision。客户端先下载到临时位置，校验 hash，再原子切换本地 revision。

Agent Forge 不提供官方 CLI。这里的本地数据库是公开源的下载产物，供各个 Agent 生态或第三方客户端读取。数据库格式和同步 manifest 会公开，但客户端命令由各 Agent 或第三方项目自行实现。

数据库和网页数据的职责不同：SQLite 适合完整离线检索；Pages 上的轻量 JSON 适合网页首屏、详情页和小范围过滤。网页不应在首次打开时下载并解析所有 SQLite 内容；需要全文检索时，由生成器输出压缩后的搜索字段或分片索引。

数据库应支持以下类型的本地查询：

    按名称、描述、关键词和版本查询
    按 Agent、类型、facet 和兼容范围过滤
    查询权限、数据处理和运行环境声明
    查询 Bundle 成员、锁定版本和有效兼容范围

## 9. 后续 API 和首期网页

### 后续读 API

    GET /api/v1/agents
    GET /api/v1/types
    GET /api/v1/packages
    GET /api/v1/packages/{packageId}
    GET /api/v1/bundles/{bundleId}
    GET /api/v1/facets
    GET /api/v1/agents/{agentId}/release

包搜索支持：

- Agent；
- 类型；
- 关键词；
- facet；
- 生命周期；
- 兼容范围；
- 游标分页。

### 后续写 API

    POST  /api/v1/packages
    PATCH /api/v1/packages/{packageId}
    POST  /api/v1/packages/{packageId}/versions
    PUT   /api/v1/packages/{packageId}/targets
    POST  /api/v1/bundles
    POST  /api/v1/bundles/{bundleId}/members
    POST  /api/v1/bundles/{bundleId}/publish
    POST  /api/v1/releases/{releaseId}/rollback

这些 API 属于后续后端阶段。首期写操作通过 Issue Form、Pull Request、Actions 和 Merge Queue 完成。

### 首期 Pages 页面

1. 包列表和 facet 搜索。
2. 跳转到 GitHub Issue Form 的包提交入口。
3. Agent 兼容性矩阵。
4. 带词汇说明的 facet 编辑器。
5. Bundle 编辑器和实时范围交集。
6. 带原因和警告的 override 编辑器。
7. 发布预览，显示受影响的 Agent/类型投影。
8. PR、Actions Checks、Merge Queue 和 Release 状态链接。
9. 审计历史和回滚。
10. Agent 和受控词汇管理。

## 10. 同步和发布流程

首期 GitHub-native 流程：

1. Issue Form 或直接 Pull Request 提交规范包文件。
2. Actions 校验字段形状、类型字段、范围语法、facet/subtype ID 和关联引用。
3. Actions 计算受影响的 Agent 和类型、Bundle Agent 交集和 facet 并集。
4. Actions 检查十分钟 cutoff、重复身份、嵌套循环和生成路径。
5. 失败时在 PR 中报告错误并阻止 Merge Queue。
6. 必需 Checks、Review 和 Merge Queue 通过后合并到 `main`。
7. Pages workflow 生成静态 JSON 和网页。
8. UTC `00:00`、`08:00`、`16:00` workflow 生成可选 SQLite 快照、hash 和 release metadata。
9. 验证所有生成结果后创建不可变 timestamp Release。
10. 最后更新 Pages 的 latest manifest；失败时保留上一个稳定页面和 Release。

后续后端流程才引入规范数据库、outbox、常驻 Worker 和动态 API；其生成校验必须复用首期的 Schema 和投影检查。

公共 manifest 不能指向未经验证的混合 revision。这里的“验证”仅指本项目自己的格式、一致性和生成流程检查，不是对上游来源的检查。

## 11. 公共免责声明

README、API 文档、生成 manifest 和网页必须保留以下含义：

> Agent Forge 是元数据整合服务。facets、兼容范围、URL、checksum、signature、advisory 和可用性字段都是被记录的声明或整合数据。Agent Forge 不检查或执行上游内容，不验证发布者声明，不认证安全性，也不保证准确性、完整性、及时性、兼容性或可用性。

## 12. 迁移步骤

1. 在文档、Schema、validator 映射和生成源名称中将 other 改为 general。
2. 用 Agent-aware source identity 替换当前固定 source 名称。
3. 将每个包关联到一个或多个 Agent target。
4. 增加 Bundle Schema 和生成逻辑。
5. 将当前 examples 转换为规范数据库种子数据。
6. 从种子数据生成公共 JSON，并与 fixture 对比。
7. 迁移完成后将公共 JSON 标记为只读生成物。
8. 修改公共布局前创建迁移版本和回滚快照。

### 生成内容的存放建议

规范数据、生成源文件和发布资产分开存放：

    agent-forge-platform/              # 私有网页、API、规范数据库和 Worker
    agent-forge/                       # Schema、词汇、发布配置和公共契约
    GitHub Pages deployment output/    # 网页和轻量静态检索数据
    GitHub Release assets/             # 按 revision 的压缩 SQLite 数据库

Pages 生成目录建议为：

site/
    |- index.html
    |- app.*                         # 美化网页前端构建结果
    '- data/
       |- manifest.json              # 当前 revision
       |- dashboard.json             # 全局 Dashboard 汇总数据
       |- agents/dsh/agent.json
       |- agents/dsh/dashboard.json  # dsh 专属统计数据
       |- agents/dsh/plugin/index.json
       |- agents/dsh/plugin/packages/<slug>/<version>.json
       |- agents/dsh/skill/index.json
       |- agents/dsh/general/index.json
       '- agents/dsh/bundle/index.json

`index.json` 只包含网页检索和导航所需的紧凑字段；完整详情按包和版本拆分。SQLite 压缩文件不复制到 Pages 的普通数据目录，而是由 manifest 指向 GitHub Release asset。

Git 仓库不应无限累积每次生成的数据库快照。建议 Git 保存 Schema、配置、manifest、生成器版本和可审查的发布记录；数据库压缩资产放在 Releases。未来若 GitHub 入口在国内或国际访问上出现实际瓶颈，再增加外部对象存储镜像。

### GitHub Pages Dashboard

Pages 增加 `/dashboard/` 页面，使用静态 `dashboard.json` 和 `agents/<agent>/dashboard.json` 渲染，不调用后端 API。Dashboard 不是管理后台，也不显示安全评分或可信度评分。

全局总览至少包含：

- 已登记 Agent 数量；
- 五种类型的包数量和版本数量；
- Plugin subtype 分布，包括 skin；
- 核心 facet 和 custom facet 使用数量；
- known/unknown Agent 兼容范围占比；
- Bundle 数量、嵌套深度和覆盖的 Agent 数量；
- 生命周期状态分布；
- 最近一次静态数据更新时间、Release timestamp 和数据版本。

每个 Agent 的 Dashboard 至少包含：

- 该 Agent 各类型包数量、版本数量和 subtype 分布；
- 类型随发布时间的数量变化；
- 能力、影响、权限、数据处理和集成 facet 的横向排行；
- unknown 兼容范围数量；
- Bundle 数量、成员数量和嵌套 Bundle 数量；
- 最近更新包列表和链接；
- 指向各类型搜索页的入口。

视觉和交互建议：

- 首屏使用总数、类型数、Agent 数、最近更新时间等简洁统计卡；
- 使用堆叠柱状图展示 Agent × 类型分布；
- 使用横向条形图展示 facet，不使用容易造成误导的“安全分数”；
- 使用时间序列图展示发布 revision 的数据变化；
- Agent 之间使用一致的颜色和图例，但颜色不能表达安全等级；
- 所有图表提供表格化或文本化替代视图，支持窄屏和键盘操作；
- 图表库必须随 Pages 静态构建产物发布，不能依赖运行时第三方 CDN；
- 图表旁显示“统计基于已整合元数据，未对来源进行核验”的免责声明。

Actions 在每次 Pages 构建时从规范文件生成 Dashboard JSON。统计生成失败时，阻止本次 Pages 发布并保留上一版页面。Dashboard 数据与搜索索引使用同一个 release timestamp。

## 13. 分阶段 TODO

### 阶段 0：审核和约束

- [x] 批准 general 替代 other。
- [x] 批准五种类型：mcp、plugin、skill、general、bundle。
- [x] 首期不把 SQLite 分发作为在线搜索依赖；保留为可选快照格式。
- [x] 首期使用 GitHub Pages 和 GitHub Releases，不引入外部对象存储。
- [x] 每 8 小时在 UTC 00:00、08:00、16:00 触发一次 CI 发布。
- [x] 包、版本、target 和 Bundle 提交至少 10 分钟后才进入公共投影；07:50 之后的变更顺延到 16:00。
- [x] dsh 是首个优先 Agent；未知兼容范围允许收录，但不能参与自动兼容计算。
- [x] 批准免责声明文字及其显示位置。
- [ ] 定义搜索延迟、数据库大小和同步时间目标。

### 阶段 1：契约和词汇表

- [x] 增加类型注册表和 Agent-aware package/source/index Schema。
- [x] 增加管理员维护的 subtype 字段；禁止用户创建 subtype 的规则写入契约。
- [x] 增加 facet Schema 结构和第一批字段。
- [x] 增加数据处理、权限、影响、运行环境和集成字段。
- [x] 增加不暗示核验的声明来源边界和免责声明字段。
- [x] 增加 Bundle 成员和 override Schema。
- [x] 增加可嵌套 Bundle 的 `memberType`/`memberId` 模型，并将循环检测列入语义校验。
- [x] 增加元数据源 manifest/index 的镜像字段：`mirrorOf`、`sourceMirrors`、`revision`、`priority` 和一致性 hash。
- [x] 增加包版本的 `distributions[]` 发行候选，区分 Git、Release、Archive 和 Registry，并明确安装器负责选择。
- [x] 增加元数据源镜像切换和发行候选选择的契约测试。
- [ ] 增加 release manifest 和投影元数据 Schema。
- [x] 更新跨文件 validator。
- [x] 将 `customFacets` 与核心 `facets` 分离，并实现提交后立即可见的 custom facet 规则。
- [ ] 增加未知 facet、非法范围、空交集和隐藏影响的负向测试。

### 阶段 2（后续）：规范后端和动态 API

- [ ] 为所有规范表创建数据库迁移；该阶段不阻塞 GitHub-native 首期。
- [ ] 实现草稿、校验、发布、退役和回滚状态。
- [ ] 实现乐观锁和幂等键。
- [ ] 在写事务中写入 outbox 事件。
- [ ] 实现 viewer、maintainer、publisher 角色；首期使用 GitHub 仓库权限和 PR Review 替代。
- [ ] 在所有写入和高危操作前重新查询 GitHub 权限；权限 API 失败时拒绝操作。
- [ ] 为所有修改和发布增加审计记录。

### 阶段 3：静态搜索和 Pages

- [x] 生成 Agent/类型轻量 JSON 索引和包详情页面。
- [x] 生成全局 `dashboard.json` 和每个 Agent 的 `dashboard.json`。
- [x] 实现名称、描述、关键词、subtype 和 facet 的浏览器端过滤。
- [ ] 对大索引按 Agent、类型和稳定分片键拆分。
- [x] 生成 README/LICENSE 链接字段。
- [ ] 用代表性数据量测量 Pages 首屏、索引加载和浏览器过滤延迟。
- [ ] 后续 API 阶段再增加服务端全文索引、游标分页和复杂兼容过滤。

### 阶段 4：投影 Worker

- [x] 按需生成每个 Agent/类型的 SQLite 数据库快照。
- [x] 在生成阶段构建 FTS 索引。
- [x] 生成静态 JSON 索引和包记录。
- [x] 生成 checksum 和 release manifest。
- [x] 实现 UTC 00:00/08:00/16:00 的 GitHub Actions 发布调度和 10 分钟 cutoff。
- [ ] 实现临时输出目录和原子发布。
- [ ] 实现重试、死信和 reconciliation 任务。
- [ ] 实现 release 回滚。

### 阶段 5：Bundle 引擎

- [ ] 为每个支持的 Agent 版本规则实现范围交集。
- [ ] 实现 Bundle-local override。
- [ ] 实现空交集错误和扩大范围警告。
- [ ] 实现成员版本和 checksum 快照。
- [ ] 实现循环依赖检测。
- [ ] 实现必选/可选成员 facet 继承。
- [ ] 为常见和冲突范围组合增加 golden tests。

### 阶段 6：GitHub Pages 和 Issue Form

- [x] 构建包浏览、搜索和详情页面。
- [x] 构建全局 Dashboard 和 Agent Dashboard 页面。
- [x] 为 Dashboard 提供图表的表格化和文本化替代视图。
- [x] 构建指向 GitHub Issue Form 的包提交入口。
- [ ] 构建 Agent 兼容性矩阵。
- [ ] 构建带说明的受控 facet 选择器。
- [ ] 构建 Bundle 编辑器和实时交集计算。
- [ ] 构建 override 确认和审计显示。
- [ ] 构建发布预览。
- [ ] 构建 PR/Action 校验结果、错误和发布状态链接页面。
- [ ] 构建审计历史和回滚控制。
- [ ] 构建 1 分钟 10 次、10 分钟 100 次提交限流、冷却、二次确认/CAPTCHA 和管理员解封流程。

### 阶段 7（后续）：客户端同步和可选快照

- [ ] 定义本地数据库位置和权限。
- [ ] 实现带条件请求的 manifest 获取。
- [ ] 实现压缩数据库下载。
- [ ] 在客户端校验生成文件的 checksum 和签名声明。
- [ ] 原子替换本地数据库 revision。
- [ ] 实现本地 facet 和全文搜索。
- [ ] Bundle 缺少成员类型快照时按需下载；在线 API 不依赖快照存在。

### 阶段 8：GitHub-native 私有预发布

- [x] 添加首批 dsh MCP 规范种子数据。
- [x] 完成 `other` 到 `general` 的迁移和 general/bundle 源目录。
- [x] 生成 Agent-specific 投影。
- [x] 将生成 JSON 与契约测试对比。
- [ ] 运行规模化搜索基准测试。
- [ ] 发布私有预览版本供审核。
- [ ] 审核通过后再替换当前源目录。
- [ ] 在 `metaone@192.168.100.197` 完成本地部署和集成测试，再开放浏览器验收。

### 阶段 9（后续）：本地服务器后端

- [ ] 在 `metaone@192.168.100.197` 部署 FastAPI、PostgreSQL、Worker 和 OAuth callback。
- [ ] 将 Git 规范数据迁移到数据库，保留 Git 生成快照和审计关联。
- [ ] 在本地浏览器验证动态 API、权限重新查询、限流和回滚流程。

## 14. 验收标准

- 一个包只创建一次，但可以投影到多个 Agent。
- 包修改后能够明确计算受影响的投影集合。
- 任意目标投影失败都不能发布部分 release。
- Bundle 范围可以由锁定的成员和 override 重现。
- Bundle override 不会修改源包声明。
- facets 可以通过 API 和本地数据库搜索。
- facets 和兼容性信息明确显示为未核验的元数据声明。
- 在线网页和 API 可以完成搜索；可选数据库快照生成后，客户端可以离线搜索。
- 客户端可以在不同 release revision 之间原子更新。
- 生成 JSON 和 SQLite 使用同一个 revision 并通过校验。
- 每次修改和发布都有审计事件。
- 回滚能够恢复旧生成 release，而不重写规范历史。
- 测试覆盖未知类型、非法 facet、非法范围、空交集、循环依赖、部分同步失败和幂等重试。
- 测试验证发布点只纳入 cutoff 前至少 10 分钟的记录，并验证 07:50 之后的记录顺延到下一发布点。
- 测试验证 GitHub 权限查询失败时写入、高危操作和发布均被拒绝。
- 测试验证 custom facet 立即进入独立属性，且不能覆盖核心 facet 或 subtype。
- 测试验证嵌套 Bundle 的 Agent 集合取成员交集、facet 取成员并集，并拒绝循环引用。
- 测试验证 Issue Form/PR/Actions/Merge Queue 的首期提交流程，不需要 PostgreSQL 或常驻后端即可完成公共数据发布。
- 测试验证 GitHub Actions 生成的 Pages JSON、Release manifest 和 GitHub Release 资产使用同一个 UTC timestamp。
- 测试验证 Dashboard 汇总、Agent 统计和搜索索引使用同一个数据 revision；统计生成失败时不发布新 Pages。

## 15. 需要审核的风险

- 多 Agent 投影会在生成物中重复兼容元数据。它简化了离线客户端，但需要测量存储和生成成本。
- 按类型拆库能减少单次更新量，但 Bundle 引用需要统一 release 协调。
- SQLite 需要稳定的读取契约和迁移策略。
- 受控词汇需要负责人、别名、弃用规则和向后兼容策略。
- 发布者声明的数据处理方式可能不完整或错误，网页绝不能把它显示为已验证的安全事实。
- 不支持的兼容范围语法必须阻止自动发布。
- 初期完整快照最简单；只有测量证明有必要时才增加增量更新。
- GitHub-native 首期无法提供真正的动态 API、数据库事务、强服务端全文搜索或 Pages 内安全 OAuth 写入；复杂管理能力应留到后端阶段。
- Issue Form 适合元数据提交，但用户体验不如专用表单；提交入口必须明确展示 GitHub 登录、PR 和公开审计语义。
- GitHub Actions 的 token 递归触发、Merge Queue 的 `merge_group` 事件和 Release/Pages 顺序需要在 CI 中单独验证。

## 附录 A：Compact 后开发交接重点

本节是下一轮开发恢复时的最小上下文。除非主人明确修改，不得把以下决策改回旧方案。

### 当前状态

- Schema v2、规范目录、validator、投影生成器、SQLite 快照生成器、Pages 网页、Dashboard、Issue Form 流程和 CI workflow 已在工作区实现并完成本地验证。GitHub 仓库配置、真实部署和后端服务仍待后续完成。
- 中文计划是主要审核版本，英文计划同步了 GitHub-native 首期方向。
- 当前仓库已迁移到 Agent-aware Schema v2；公共类型为 `mcp`、`plugin`、`skill`、`general`、`bundle`，旧 `other` 目录已迁移为 `general`。
- 当前仓库中的 `HANDOFF-STAGE-H.md` 和 `docs/` 属于工作区新增文档；不要误删或覆盖。
- 尚未连接开发服务器。之前的 SSH 环境探测因权限审批服务 404 没有执行；不得绕过审批重试。

### 不可改变的首期方向

1. 首期不购买或依赖后端服务器，不使用 PostgreSQL、FastAPI、常驻 Worker 或动态 API。
2. Git 仓库中的规范 JSON/YAML 是首期唯一事实来源；Pull Request 是唯一写入口。
3. GitHub Pages 负责网页、Dashboard、静态搜索 JSON、包详情和 Issue Form 提交入口。
4. GitHub Actions 负责 Schema/语义校验、生成预览、Pages 数据和 Release 资产。
5. GitHub Merge Queue 和分支保护负责合并门禁。
6. SQLite 是可选快照，不是首期在线搜索依赖；在线搜索使用 Pages 静态 JSON。
7. 后端数据库/API/Worker 作为后续阶段，未来可从 Git 规范文件迁移，但必须复用相同 Schema 和投影规则。

### 公共类型和字段

- 五种类型：`mcp`、`plugin`、`skill`、`general`、`bundle`。
- `general` 替代旧的 `other`，仍要求 `toolType` 和 `agentUse`；不使用 `typeRef.type`。
- 每个类型都有可选、管理员维护的 `subtype`；用户不能创建或覆盖 subtype。
- 包创建时最多选择一个 subtype，并复制到每个 Agent/type 投影。
- Skin 使用 `plugin` + `subtype=skin`，不要新增一级 `skin` 类型。
- README 和 LICENSE 只保存链接：`links.readme`、`links.license`；不复制正文、不下载审查。
- 用户 facet 立即生效，但独立放在 `customFacets`；不能覆盖核心 `facets`，并显示“用户定义、未核验”。
- 未知 Agent 兼容范围使用 `agentVersionRange: null`、`compatibilityStatus: unknown`，可以展示但不能参与自动兼容过滤或 Bundle 计算。

### Bundle 规则

- Bundle 允许嵌套。
- 成员不分必选/可选，统一作为成员处理。
- 成员模型必须使用 `memberType=package|bundle` 和 `memberId`，不能只使用 `packageVersionId`。
- 元数据源镜像与插件发行候选必须分层：前者在 source manifest/index 中，后者在包版本/target 的 `distributions[]` 中。
- Bundle 的 Agent 集合取所有成员 Agent 集合的交集。
- Bundle 的 capabilities、effects、dataPractices、permissions、runtime、integrations 取所有成员并集。
- 禁止循环引用。
- 所有类型共用 dependency/conflict 机制；Bundle 只是特化类型。
- Agent Forge 只提供查询元数据，不决定安装目标或安装步骤。

### 提交流程和时间规则

- Pages 的提交按钮跳转 GitHub Issue Form；维护者也可直接创建 PR。
- Actions 校验 Schema、重复身份、facet、subtype、Bundle 交集、嵌套循环和生成路径。
- Issue Form Action 创建 PR 不得依赖普通 `GITHUB_TOKEN` 触发后续 workflow；使用受限 GitHub App token 或 `workflow_run`。
- Merge Queue workflow 必须监听 `merge_group` 事件。
- 包、版本、target、Bundle 的公共投影至少延迟 10 分钟。
- GitHub Actions 仅在 UTC `00:00`、`08:00`、`16:00` 发布，cron 为 `0 0,8,16 * * *`。
- 记录必须在发布点前至少 10 分钟通过校验，否则顺延到下一个发布点。
- 无变更不创建空 Release。
- Release 使用统一 UTC timestamp，例如 `dsh-plugin-20260930T160000Z.db.zst`；不覆盖旧资产。
- 生成所有投影、校验和 Release asset 后，最后才更新 Pages latest manifest。

### 权限和反滥用

- 首期身份依赖 GitHub 账号、PR 作者、Review 和仓库权限，不在 Pages 保存写入 token。
- 每次写入和高危操作前重新查询仓库权限；GitHub 权限 API 失败必须拒绝操作，不使用旧缓存授权。
- 用户失去仓库权限后，已有内容保留；禁止继续编辑；其他授权维护者可通过新审计版本回退，不能重写历史。
- 初始限流：1 分钟 10 个、10 分钟 100 个包提交；超限进入冷却、二次确认/CAPTCHA 和管理员解封流程。
- 重新 OAuth 不是唯一反滥用证明。

### Dashboard 必须保留

- 生成 `data/dashboard.json` 和 `data/agents/<agent>/dashboard.json`。
- 展示类型、subtype、facet、兼容状态、Bundle、生命周期和更新时间统计。
- 提供图表的表格/文本替代视图、响应式布局和键盘访问。
- 不显示安全分数、可信度分数或任何来源核验暗示。
- Dashboard、搜索索引和 Release 使用同一个数据 timestamp；统计生成失败阻止 Pages 发布。

### 下一轮首个开发顺序

1. [x] 重写四份公共 Schema 为 Agent-aware v2，并加入 `subtype`、`customFacets`、链接字段、unknown compatibility、Bundle member 模型、元数据源镜像和 `distributions[]`。
2. [x] 编写规范 JSON 文件布局和初始 dsh Agent 配置。
3. [x] 实现本地 validator：Schema、跨文件身份、镜像 revision、投影和路径一致性。
4. [x] 编写四份 Schema 的中英文字段 README 和完整 TypeScript 风格示例。
4. 实现 GitHub Actions：PR 校验、`merge_group` 校验、Pages 构建、UTC 三时点 Release 和 latest manifest 更新。
5. 实现静态搜索页、包详情页和 Dashboard。
6. 用 examples 迁移种子数据，先生成 dsh 的 mcp/plugin/skill/general/bundle 投影。
7. 补齐契约测试、生成快照测试、PR 流程测试和 Pages 构建测试。
8. 只有 GitHub-native 首期通过验收后，才讨论本地服务器上的后端阶段。

## 16. 根据主人确认的调整

### 首批 Agent 和扩展方式

- 首个优先支持的 Agent 是 dsh（DeepSeek Harness）。
- 后续 Agent 按可获得的公开资料逐步加入。
- Agent 注册记录必须包含资料来源字段，但资料来源只被整合，不被 Agent Forge 核验。
- 当 Agent 数量较大时，先按数量级建立基准数据，再决定是否需要进一步的数据库分片或增量同步。
- 当前 GitHub 仓库 owner 为 `metaone01`，仓库名为 `agent-forge`，默认分支为 `main`；开发阶段保持私有。

### 后端技术方案

默认采用以下实现，除非实现阶段发现明确的技术阻塞：

    Backend: Python + FastAPI
    ORM and migrations: SQLAlchemy + Alembic
    Canonical database: PostgreSQL
    Projection worker: Python 独立 Worker
    Local source database: SQLite + FTS5
    Frontend: TypeScript web application

第一版不额外引入消息队列。同步任务使用 PostgreSQL outbox 和任务表；只有任务吞吐量实测不足时，才引入独立队列服务。

### 首期 GitHub-only 发布方案

首期只使用 GitHub：

- GitHub Releases 按固定时间窗口和不可变的时间戳 release ID 发布压缩 SQLite 数据库，例如 `dsh-plugin-20260930T095900Z.db.zst`；不覆盖已有 release asset。
- GitHub Pages 提供美化网页、公共免责声明、Agent/类型导航、轻量搜索索引、包详情 JSON 和最新版 manifest。
- 网页默认检索轻量索引，不在首次打开时下载完整 SQLite；数据库文件提供下载链接，供 Agent 生态和第三方客户端同步。
- 每个 manifest 包含 release revision、数据库大小、SHA-256、下载地址和生成时间。
- Release 页面只保存版本化数据库资产，不作为规范数据库或人工编辑入口。

发布按固定时间调度，不按单个包或人工批次触发。GitHub Actions 使用 UTC `00:00`、`08:00`、`16:00` 三个发布点，对应 cron `0 0,8,16 * * *`。每个发布点只纳入至少提前 10 分钟提交且已通过本项目格式和关联校验的记录；例如 07:55 之后提交的记录顺延到 16:00。没有变更时不创建空 Release。发布延迟是公开可见时间控制，不表示来源已经被检查。

GitHub 没有通用 S3 式对象存储。首期不引入外部对象存储，直接使用 GitHub Releases 资产和 GitHub Pages 静态文件。国内访问速度、稳定性和带宽达到实际瓶颈后，再评估阿里云 OSS、腾讯云 COS 或其他对象存储镜像；外部镜像属于后续扩展，不是首期依赖。

### 生成的 Agent 数据内容

每个 Agent、每个类型生成的只读数据库和静态 JSON 至少包含：

1. 包身份：稳定包 ID、名称、显示名、描述、许可证名称、主页、仓库、README 链接、LICENSE 链接和文档链接。
2. 包版本：原始版本字符串、版本类型、发布说明、发布时间。
3. Agent 目标：Agent ID、工具类型、适用 Agent 版本范围和 Agent 专属 target 元数据。
4. 发现字段：关键词、支持的平台、生命周期状态和检索用摘要。
5. Facet：核心 facet，以及被允许进入公共投影的用户 facet。
6. 权限和行为：文件系统、网络、进程、凭据、配置和远程资源等声明。
7. 数据处理：数据类别、动作、用途、目的地、保留说明和 required 标记。
8. 安装描述：命令、URL、API 或脚本说明；不执行这些内容。
9. 完整性材料：checksum、signature、镜像和链接字段；这些只作为被记录的材料，不表示已验证。
10. 依赖和生命周期：依赖、冲突、替代、弃用、归档和预发布状态。
11. 安全与 advisory：上游提供的安全声明或 advisory 引用；不作安全结论。
12. 生成信息：Agent、类型、Schema 版本、release revision、生成时间和生成文件 hash。

数据库不包含二进制安装包、大型原始文档或审计内部字段。详情 URL 指向静态 JSON 或上游地址。操作人、编辑历史、失败日志和内部任务信息只保存在后端审计系统，除非以后明确决定公开。

### Skin 与 Plugin 的关系

第一阶段不新增独立的 skin 一级类型。Skin 仍归入 `plugin`，但增加明确的子类型和 facet：

    plugin.subtype = skin
    capabilities: ui.theme, gui.modify 或 ui.customize
    integrations: agent-ui 或 desktop

这样可以复用 Plugin 的版本、Agent 兼容、安装和发布流程，同时在网页中按 `skin` 筛选。只有在 Skin 的安装方式、生命周期、兼容性或发布权限与 Plugin 实际不同，导致同一套 Schema 无法清晰表达时，才考虑把 skin 升级为新的一级类型。

建议区分以下情况：

- 只改变颜色、图标、字体或布局：`plugin.subtype=skin`；
- 修改 Agent UI 行为或提供桌面控件：仍是 `plugin`，增加 `gui.modify`、`ui.customize` 等 facet；
- 纯资源包但被 Plugin 加载：仍可作为 Plugin 的 skin 目标记录；
- 同时改变行为和外观：仍是 Plugin，可同时拥有普通 Plugin 和 skin 相关 facet。

### GitHub OAuth 和仓库权限

网页使用 GitHub OAuth 识别用户。登录本身不等于编辑权限，后端还要查询用户对目标 agent-forge 仓库的实际权限。

建议的权限映射：

    admin       -> Admin
    maintain    -> Publisher
    push        -> Maintainer
    triage      -> Reviewer 或只读审核者
    pull        -> Viewer
    无权限      -> 只能访问公开页面

后端必须在服务端检查仓库权限，不能相信前端传来的角色。仓库名称、所属组织、默认分支和是否私有需要在实现前配置确认。

建议使用 GitHub OAuth 负责登录和身份关联，使用后端服务身份或 GitHub App 负责生成文件提交、分支和发布。网页点击发布后，后端创建发布分支和 Pull Request，不直接写入受保护的 `main`；合并由仓库保护规则或发布者审核完成。这样不需要长期保存每个用户的宽权限写入 token，同时审计记录仍然保留真实操作人。

### 用户自定义 facet

用户可以创建自定义 facet，但它们不能和管理员核心 facet 混在同一个词汇空间中。

建议使用独立属性和命名空间：

    coreFacets       # 管理员维护的稳定公共词汇
    customFacets     # 用户创建的扩展声明

每个 custom facet 至少包含：

    id
    namespace
    label
    description
    creator
    visibility
    status
    createdAt

命名空间建议使用用户或组织前缀，例如：

    user.<userId>/workflow.review
    org.<orgId>/domain.research

custom facet 不得覆盖或伪装成 core facet。公共投影中必须保留创建者命名空间，并显示“用户定义、未核验”状态。

默认建议：

- private：只有创建者可见；
- shared：授权的维护者或团队可见；
- public：可以进入网页搜索和公共数据库，但仍标记为用户定义声明。

是否允许用户直接发布 public custom facet、是否需要管理员批准，是实现前仍需确认的权限策略。

当前决定为：custom facet 可以在提交成功并通过格式校验后立即生效，但它始终存放在独立的 `customFacets` 属性中，并显示“用户定义、未核验”。使用该 facet 的包记录仍遵守包公共投影的 10 分钟最短延迟；facet 词汇本身不等待发布窗口。

建议的提交速率控制：

- 同一用户 1 分钟最多创建或发布 10 个包；
- 同一用户 10 分钟最多创建或发布 100 个包；
- 达到阈值时，立即阻断新的发布任务并进入冷却期，不仅要求重新登录；
- 重新 OAuth 只能刷新身份会话，不能作为唯一的反滥用证明；
- 触发后要求重新授权会话、二次确认或 CAPTCHA，并保留管理员人工解封路径；
- 同时按 GitHub 账号、内部用户 ID、IP/设备信号和仓库动作记录限流，但 IP 只能作为辅助信号，不能单独决定处罚；
- 发布队列继续执行已通过校验的任务，但在冷却期内不让新的包进入公共投影；
- 限流阈值应配置化，第一版可按上述 10/1 分钟和 100/10 分钟开始，再用真实误报率调整。

这比单纯“重新登录后继续”更合适：大量提交可能来自自动化脚本、账号被盗或误操作，重新登录本身不能区分三者。

“包提交”指创建或更新包、版本、Agent target、Bundle 成员和 Bundle 发布草稿的写操作；facet 词汇创建单独计数。以下高危操作必须在操作开始前重新查询 GitHub 仓库权限：删除/退役、回退、发布、修改 Agent 或核心 facet、修改生成配置。权限 API 出错时直接返回错误，不能使用旧缓存继续执行。

### 未知 Agent 兼容版本

对没有可靠适配版本号的包，允许收录，但兼容范围必须使用显式未知值，而不是空字符串：

    agentVersionRange: null
    compatibilityStatus: unknown
    compatibilityNote: "上游未提供可解析的 Agent 版本范围"

未知范围的规则：

- 可以出现在包详情和人工搜索结果中；
- 不进入“兼容某个 Agent 版本”的确定性过滤结果；
- 不能参与 Bundle 的自动范围交集；
- Bundle 若包含必选的未知范围成员，默认不能自动发布，除非用户在 Bundle 中显式 override 并填写原因；
- 用户可以在后续补充范围，补充后重新生成受影响投影。

### 最新发布、权限和 Bundle 决策

- 发布任务只在 UTC `00:00`、`08:00`、`16:00` 由 GitHub Actions 触发；同一时间窗口生成一个统一 timestamp release。SQLite 资产是可选产物，Pages 的在线 JSON/API 不依赖它。
- 包、版本、target、Bundle 和 public custom facet 的公共可见时间统一为提交成功后至少 10 分钟；延迟用于反滥用，不代表内容已经被验证。
- 初始限流为同一身份 1 分钟最多 10 个、10 分钟最多 100 个包提交。达到阈值时阻断新的发布任务，要求重新确认身份或 CAPTCHA，并允许管理员解封；重新 OAuth 不是唯一证明。
- GitHub 仓库权限在每次写入和高危操作前重新查询。权限查询 API 失败时直接拒绝操作，不使用旧权限缓存继续执行。
- 用户离开仓库或权限下降后，不删除其已经产生的公开元数据。记录保留、后续修改权限撤销；每次修改可由有权限的其他维护者通过审计版本回退，回退本身生成新版本，不重写历史。
- 所有类型共享依赖和冲突机制。Bundle 是一种特化类型，不负责安装；它允许嵌套，成员 Agent 集合取交集，facet 取所有成员并集。
- custom facet 直接生效，但只能进入独立的 `customFacets` 属性；它们不得覆盖或伪装成核心 `facets`。

### 对原方案的必要修正

1. 不再把 SQLite 数据库分发当作首期必做。在线搜索优先使用 API 和 Pages 轻量 JSON，快照只在离线或性能需求出现时发布。
2. 不再使用“离开仓库后删除或冻结全部内容”的思路。删除会破坏引用和历史；采用保留数据、撤销后续编辑权、审计回退生成新版本的方式。
3. 不把“重新 OAuth”当作反滥用的充分条件。被盗账号也能重新授权，必须结合限流、冷却、CAPTCHA、审计和管理员解封。
4. Bundle 成员不能只引用 packageVersionId，必须绑定到具体成员记录并允许 `memberType=package|bundle`，否则无法正确计算 Agent 交集和嵌套 Bundle。

## 17. 实施前仍需确认的问题

以下问题会影响发布和授权实现，建议在开始编码前确定：

1. push 权限是否足以发布，还是只有 maintain/admin 可以发布；是否需要单独的 Publisher 角色。
2. dsh 的第一个版本规则和兼容性资料来源是什么；目前未知范围可以先收录，但不能参与自动兼容计算。
3. 是否需要在公共数据中保留创建者显示名、facet 创建时间和来源说明。
4. 当生成数据超过 Git/GitHub Pages 的合适范围时，选择哪一个外部对象存储；第一阶段不需要现在就引入。
