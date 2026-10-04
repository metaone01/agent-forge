# `package.schema.json` 字段说明

## 用途

此 Schema 描述一个包的一个版本。它只保存元数据，不保存二进制文件，也不验证来源、兼容性、checksum、signature、facet 或安装命令。`bundle` 是特殊包类型：它可以没有 `distributions`，因为成员的发行候选由安装器分别选择。

## 顶层字段

| 字段 | 类型/取值 | 作用 |
| --- | --- | --- |
| `schemaVersion` | `2` | Schema 契约版本。 |
| `id` | string | 跨版本、Agent 和投影稳定的逻辑包 ID。 |
| `name` | string | 保存原始 Unicode 包名或源内名称，中文不替换为连字符。 |
| `displayName` | string | 展示名称。 |
| `version` | string | 上游原始版本字符串，不改写。 |
| `versionScheme` | `semver`/`npm`/`pep440`/`calver`/`date`/`custom`/`unknown` | 版本比较方案提示，不是保证。 |
| `description` | string | 包描述。 |
| `releaseNotes` | string | 该版本发布说明。 |
| `license` | string 或 string[] | 上游提供的许可证声明。 |
| `media` | object | 可选图标和有序静态预览图，仅保存未核验的图片引用。 |
| `links` | object | repository、homepage、readme、license、documentation、changelog、issues 链接。只索引链接，不复制正文。 |
| `keywords` | string[] | 自由文本搜索词。 |
| `maintainers` | object[] | 维护者姓名、URL、email 和角色。 |
| `platform` | object | OS、架构、libc、runtime 提示。 |
| `type` | `mcp`/`plugin`/`skill`/`general`/`bundle` | Agent Forge 一级分类。 |
| `subtype` | string 或 null | 管理员定义的单个子类型；用户不能创建或覆盖。 |
| `mcpDetails` | object | `mcp` 专用字段，含 registryType、identifier、transport 等。 |
| `pluginDetails` | object | `plugin` 专用字段，含 manifestPath、sourceType 等。 |
| `skillDetails` | object | `skill` 专用字段，含 skillPath、allowedTools 等。 |
| `generalDetails` | object | `general` 专用字段，必须含 `toolType` 和 `agentUse`。不使用 `kind` 或 `typeRef.type`。 |
| `bundleDetails` | object | `bundle` 专用成员、有效目标和 facet 并集。 |
| `targets` | object[] | 每个 Agent 的兼容范围和目标信息。unknown 必须使用 null 范围。 |
| `distributions` | object[] | 包本体的 Git、Release、Archive、Registry、OCI 或其他发行候选。非 Bundle 类型至少一个。 |
| `dependencies` | object[] | 依赖。所有类型都可使用。 |
| `conflicts` | object[] | 冲突声明。 |
| `provides` | object[] | 此包提供的逻辑能力或包 ID。 |
| `replaces` | object[] | 此包替代的逻辑包。 |
| `facets` | object | 管理员词汇：capabilities、effects、dataPractices、permissions、runtime、integrations。 |
| `customFacets` | object | 用户自定义 facet，与核心 facet 分离并标记未核验。 |
| `lifecycle` | object | active、deprecated、archived、yanked、experimental 等状态。 |
| `createdAt`/`updatedAt`/`publishedAt` | date-time | 生命周期时间戳。 |
| `_meta` | namespaced object | 供消费者扩展的命名空间数据，Agent Forge 不解释。 |

## 复用定义

`identifier` 是稳定 ID；`packageName` 是名称；`url` 是 URI；`timestamp` 是 ISO 8601 date-time；`stringOrArray` 是一个字符串或非空字符串数组；`stringList` 是去重字符串数组。

`agentTarget` 使用 `agentId`、`compatibilityStatus`、`agentVersionRange`、`versionScheme`、`compatibilityNote`、`targetMetadata`、`installMetadata` 和 `status`。`known` 必须提供范围，`unknown` 必须提供 null 范围和说明。

`distribution` 使用 `id`、`type`、`url`，可选 `agentIds`、`version`、`ref`、`registry`、`priority`、`regions`、`checksum`、`signature`、`install` 和 `notes`。`install` 内含 `type`、`url`、`command`、`scriptIntegrity`、`requires` 和 `notes`；它只是安装说明。

可选 `name` 是发行候选的显示名称，例如 `github repository`、`npm package`；消费者展示时优先使用 `name`，缺失时回退到 `type`。来源明确时，`type` 使用 `github-repo`、`npm-pkg` 等具体来源标识。`github-repo` 延续仓库/`ref` 语义，`npm-pkg` 延续注册表/`version` 语义；`registry=npm` 保留注册表身份。其它来源和旧记录仍接受 `git`、`registry`。插件的 `pluginDetails.sourceType` 同样支持这两个具体标识。消费者必须支持新增类型后才能使用迁移后的数据，不能只按旧枚举分派安装逻辑。

其它具体来源包括 `pypi-pkg`、`nuget-pkg`、`crates-pkg`、`ghcr-image`、`dockerhub-image`、`quay-image`、`gcp-artifact-image`、`oci-image`、`github-mcpb`、`gitlab-mcpb`、`mcpb-pkg` 和 `mcp-endpoint`。远程端点仅表示 MCP 服务地址，不是文件下载；MCPB 是文件格式，不是一级 `bundle` 分类。

包 `name` 和 index 的包名键允许 Unicode，控制字符禁止。`id` 允许 Unicode 和 UTF-8 百分号编码；新增采集记录将原始身份可逆编码为 ID，例如 `plugin.author/project/%E4%B8%AD%E6%96%87`。已有 ID 保持稳定。JSON 中的 `\u4e2d\u6587` 是“中文”的序列化写法，解码后不是另一个身份。文件路径的编码与包名分离，见[中文名称处理](../unicode-package-names-2026-10-02.md)。

`bundleMember` 使用 `memberType`（`package` 或 `bundle`）和 `memberId`，可带版本范围、Agent 和 Bundle-local `override`。`effectiveTarget` 保存交集计算结果；`facetUnion` 和 `customFacetUnion` 分别保存核心与用户 facet 并集。禁止循环引用。

## TypeScript 风格完整示例

下面的示例刻意填充所有可同时出现的顶层字段和发行候选字段。由于 Schema 要求五种类型详情互斥，不存在一个同时填写 `mcpDetails`、`pluginDetails`、`skillDetails`、`generalDetails` 和 `bundleDetails` 仍然合法的实例；因此其余四个详情对象在字段表和最后的分支说明中分别覆盖。

```typescript
const record = {
  schemaVersion: 2, // 当前公共契约版本。
  id: "dsh.example-skin", // 跨版本和投影稳定的逻辑 ID。
  name: "example-skin", // 源内包名。
  displayName: "Example Skin", // 网页显示名。
  version: "1.4.0", // 上游原始版本。
  versionScheme: "semver", // 仅作为比较提示。
  description: "A GUI skin plugin for DSH.", // 人类可读描述。
  releaseNotes: "Adds compact navigation icons.", // 本版本说明。
  license: "MIT", // 上游许可证声明，不代表法律核验。
  links: { // 所有链接都只做索引。
    repository: "https://github.com/example/example-skin", // 源码仓库。
    homepage: "https://example.com/example-skin", // 项目主页。
    readme: "https://github.com/example/example-skin#readme", // README 链接。
    license: "https://github.com/example/example-skin/blob/v1.4.0/LICENSE", // LICENSE 链接。
    documentation: "https://example.com/example-skin/docs", // 文档链接。
    changelog: "https://github.com/example/example-skin/blob/v1.4.0/CHANGELOG.md", // 变更记录。
    issues: "https://github.com/example/example-skin/issues", // 问题追踪。
  },
  keywords: ["gui", "skin", "desktop"], // 自由文本搜索词。
  maintainers: [{ // 维护者声明。
    name: "Example Maintainer", // 姓名。
    url: "https://github.com/example", // 主页。
    email: "maintainer@example.com", // 联系地址。
    role: "maintainer", // 角色。
  }],
  platform: { // 平台提示，不是可用性保证。
    os: ["linux", "windows"], // 操作系统。
    arch: ["x86_64", "arm64"], // CPU 架构。
    libc: ["glibc"], // libc。
    runtime: ["nodejs>=20"], // 运行时。
  },
  type: "plugin", // 一级分类。
  subtype: "skin", // 管理员定义的唯一 subtype。
  pluginDetails: { // plugin 专用信息。
    manifestPath: ".dsh/plugin.json", // 插件 manifest 路径。
    sourceType: "github-repo", // 上游来源平台与形式。
    marketplaceUrl: "https://example.com/marketplace/example-skin", // 市场页。
    entrypoint: "dist/index.js", // 入口。
    permissions: ["filesystem.workspace-read"], // 上游声明的权限。
  },
  targets: [{ // Agent 兼容目标。
    agentId: "dsh", // Agent 稳定 ID。
    agentVersionRange: "^0.2.0", // 可解析范围。
    versionScheme: "semver", // Agent 版本规则。
    compatibilityStatus: "known", // 已提供可解析范围。
    targetMetadata: { desktop: true }, // 目标专属元数据。
    installMetadata: { mode: "plugin" }, // 安装器可用提示。
    status: "active", // 目标生命周期。
  }, {
    agentId: "future-agent", // 另一个 Agent。
    agentVersionRange: null, // 未提供可解析范围时必须为 null。
    versionScheme: "unknown", // 不确定版本方案。
    compatibilityStatus: "unknown", // 只允许展示，不参与自动交集。
    compatibilityNote: "Upstream did not publish a parseable range.", // 必填说明。
    targetMetadata: {}, // 可为空对象。
    installMetadata: {}, // 可为空对象。
    status: "experimental", // 目标状态。
  }],
  distributions: [{ // 插件本体发行候选，不是元数据镜像。
    id: "github-release", // 候选稳定 ID。
    type: "release", // 发行方式。
    url: "https://github.com/example/example-skin/releases/download/v1.4.0/example-skin.zip", // 下载或访问地址。
    agentIds: ["dsh"], // 适用 Agent。
    version: "1.4.0", // 候选版本。
    ref: "v1.4.0", // Git/Release 引用。
    registry: "github", // 发行平台名称。
    priority: 10, // 数字越小优先级越高。
    regions: ["global"], // 地区提示。
    checksum: { sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }, // 声明的摘要。
    signature: { // 声明的签名材料。
      url: "https://github.com/example/example-skin/releases/download/v1.4.0/example-skin.zip.sig", // 签名地址。
      type: "sigstore", // 签名类型。
      keyId: "example-key", // 可选 key ID。
      keyUrl: "https://example.com/keys/example.asc", // 可选 key 地址。
    },
    install: { // 安装提示，不自动执行。
      type: "url", // 安装提示类型。
      url: "https://github.com/example/example-skin/releases/download/v1.4.0/example-skin.zip", // 安装地址。
      command: "dsh plugin install example-skin", // 示例命令。
      scriptIntegrity: "sha256-BASE64-VALUE", // 脚本完整性声明。
      requires: ["dsh>=0.2.0"], // 前置条件。
      notes: "Review the upstream instructions before execution.", // 注意事项。
    },
    notes: "Primary global distribution.", // 候选备注。
  }],
  dependencies: [{ // 普通依赖。
    id: "dsh.ui-runtime", // 依赖 ID。
    versionRange: "^0.2.0", // 依赖版本范围。
    versionScheme: "semver", // 范围规则。
    agentId: "dsh", // 依赖适用 Agent。
    optional: false, // 是否可选。
    reason: "Provides the desktop extension host.", // 依赖原因。
  }],
  conflicts: [{ id: "dsh.legacy-theme", versionRange: "<2.0.0", versionScheme: "semver", agentId: "dsh", optional: false, reason: "Both replace the same theme slot." }], // 冲突声明。
  provides: [{ id: "dsh.gui.skin", versionRange: "1.4.0", versionScheme: "semver", agentId: "dsh", optional: false, reason: "Provides the skin capability." }], // 提供的能力。
  replaces: [{ id: "dsh.example-skin-old", versionRange: "*", versionScheme: "custom", agentId: "dsh", optional: false, reason: "Successor package." }], // 替代关系。
  facets: { // 管理员维护的受控 facet。
    capabilities: ["gui.modify"], // 能力。
    effects: ["user-config.modify"], // 影响。
    dataPractices: ["public:read"], // 数据类别和动作声明。
    permissions: ["filesystem.workspace-read"], // 权限。
    runtime: ["interactive.required"], // 运行环境。
    integrations: ["agent-ui"], // 集成方式。
  },
  customFacets: { // 用户自定义、未核验 facet。
    capabilities: ["theme.preview"], // 自定义能力。
    effects: ["desktop.appearance.modify"], // 自定义影响。
    dataPractices: ["telemetry:unknown"], // 自定义数据声明。
    permissions: [], // 可为空。
    runtime: ["gpu.optional"], // 自定义运行提示。
    integrations: ["community-dashboard"], // 自定义集成。
    other: ["community-entered"], // 其他自定义标签。
  },
  lifecycle: { // 生命周期。
    status: "active", // 当前状态。
    reason: "Maintained upstream.", // 状态原因。
    replacementId: "dsh.example-skin-next", // 可选替代包。
    since: "2026-09-30T16:00:00Z", // 状态生效时间。
  },
  createdAt: "2026-09-01T00:00:00Z", // 创建时间。
  updatedAt: "2026-09-30T15:00:00Z", // 更新时间。
  publishedAt: "2026-09-30T16:00:00Z", // 公共发布时间。
  _meta: { // 命名空间扩展，项目不解释其值。
    "org.example/review": { importedBy: "community" }, // 示例扩展。
  },
} as const;
```

`mcpDetails`、`skillDetails`、`generalDetails` 和 `bundleDetails` 的字段见上面的定义说明；它们必须与顶层 `type` 匹配。`generalDetails.toolType` 是 General 工具的具体性质，不是新的一级分类。Bundle 的成员 `memberType` 只能是 `package` 或 `bundle`，其有效 Agent 集合取成员交集，facet 取成员并集。

## 展示媒体

可选 `media.icon` 要求绝对、无用户名/密码的 HTTPS `url`（最多 4096 字符）和非空白 `alt`（1–500 字符）。`media.previews` 为有序的 1–12 张静态图片，每项要求相同字段，可选 `theme=light|dark|system`；首张为默认预览。`media` 至少有图标或预览，不允许额外字段。skin 仍用 `plugin + subtype=skin`。图片引用未经核验，不代表兼容性；目录工具不下载、缓存或代理图片，不运行皮肤代码。

索引 `packageEntry.media` 只复制图标与至多首张预览，完整画廊以所选记录为准。Pages 默认不加载外链图片，用户在本次页面会话中允许后才懒加载，并设置 no-referrer 和固定失败占位。导入来源保存在 `_meta["org.agentforge/media-provenance"].sources`。

此扩展保持 v2，但旧的封闭式验证器会拒绝新字段；必须先升级 package/index 验证器和严格消费者，再发布带媒体的记录。回填审阅与部署顺序见仓库中的 `docs/MEDIA-MIGRATION.zh-CN.md`。
