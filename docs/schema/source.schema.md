# `source.schema.json` 字段说明

## 用途

Source manifest 描述一个可独立消费的 Agent Forge 元数据目录。它可以是某个 Agent/type 的投影，也可以是全 Agent 目录。它只描述元数据地址，不代表插件本体镜像。

## 字段

| 字段 | 作用 |
| --- | --- |
| `schemaVersion` | 固定为 `2`。 |
| `sourceId` | 稳定机器 ID，例如 `agent-forge:dsh:plugin`。 |
| `name` | 人类可读名称。 |
| `agentId` | 覆盖的 Agent；`null` 表示全 Agent 目录。 |
| `type` | `mcp`、`plugin`、`skill`、`general` 或 `bundle`。 |
| `baseUrl` | manifest 和 index 所在的基准 URL。 |
| `index` | 相对 `baseUrl` 的索引路径。 |
| `revision` | manifest、index 和生成资产共享的不可变 revision。 |
| `generatedAt` | manifest 生成时间。 |
| `updatedAt` | 目录数据最后变更时间。 |
| `mirrorOf` | 镜像的 canonical `sourceId`；主源为 null 或省略。 |
| `priority` | 消费者选择优先级，数值越小越优先。 |
| `official` | 运营者声明是否为官方端点，不是安全证明。 |
| `description` | 目录说明。 |
| `indexChecksum` | index 字节摘要声明。 |
| `sourceMirrors` | 提供同一 revision 的元数据镜像端点，含 sourceId、url、mirrorOf、revision、priority、region、official。 |
| `relatedSources` | 相关目录端点，含 sourceId、url、relation、revision、priority、region。 |
| `_meta` | 命名空间扩展。 |

`sourceMirrors` 只用于切换 Agent Forge 元数据源；它不表示 `package.schema.json` 中的插件发行地址。revision 不一致时，消费者应报告冲突或按明确优先级选择。

## TypeScript 示例

```typescript
const source = {
  schemaVersion: 2, // Source manifest 契约版本。
  sourceId: "agent-forge:dsh:plugin", // 稳定源 ID。
  name: "Agent Forge DSH Plugins", // 人类名称。
  agentId: "dsh", // 本目录的 Agent 投影。
  type: "plugin", // 本目录的包类型。
  baseUrl: "https://metaone01.github.io/agent-forge/data/dsh/plugin/", // 基准地址。
  index: "index.json", // 相对索引路径。
  revision: "20261001T000000Z", // 不可变发布时间戳。
  generatedAt: "2026-10-01T00:00:00Z", // 生成时间。
  updatedAt: "2026-09-30T23:45:00Z", // 数据更新时间。
  mirrorOf: null, // 主源没有 canonical mirrorOf。
  priority: 0, // 首选源。
  official: true, // 运营者声明；不是安全保证。
  description: "Read-only DSH plugin metadata.", // 说明。
  indexChecksum: { sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }, // index 摘要声明。
  sourceMirrors: [{ // 同 revision 的元数据镜像。
    sourceId: "agent-forge-cn:dsh:plugin", // 镜像源 ID。
    url: "https://mirror.example.cn/agent-forge/data/dsh/plugin/", // 镜像 URL。
    mirrorOf: "agent-forge:dsh:plugin", // 主源 ID。
    revision: "20261001T000000Z", // 镜像 revision。
    priority: 10, // 备用优先级。
    region: "cn", // 地区提示。
    official: false, // 不是官方端点声明。
  }],
  relatedSources: [{ // 相关但不一定等价的源。
    sourceId: "agent-forge:dsh:bundle", // 相关源 ID。
    url: "https://metaone01.github.io/agent-forge/data/dsh/bundle/", // 相关源 URL。
    relation: "regional", // 关系类型。
    revision: "20261001T000000Z", // 可选 revision。
    priority: 20, // 选择优先级。
    region: "global", // 地区。
  }],
  _meta: { "org.example/catalog": { generatedBy: "pages" } }, // 扩展。
} as const;
```
