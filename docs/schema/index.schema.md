# `index.schema.json` 字段说明

## 用途

Index 是一个 source revision 的紧凑包索引。它用于低带宽发现、版本列表和详情路径定位，不包含插件二进制。manifest 的 `indexChecksum` 可以记录 index 的摘要，避免 index 自己记录自己的摘要而形成循环。

## 字段

| 字段 | 作用 |
| --- | --- |
| `schemaVersion` | 固定为 `2`。 |
| `sourceId` | 必须与 source manifest 一致。 |
| `sourceManifest` | 指向 source manifest 的相对路径。 |
| `sourceUrl` | 可选的 manifest URL。 |
| `agentId` | 必须与 manifest 一致；null 表示全 Agent。 |
| `type` | 必须与 manifest 一致的五种类型之一。 |
| `revision` | 必须与 manifest 一致。 |
| `generatedAt`/`updatedAt` | 生成和数据更新时间。 |
| `ttl` | 建议刷新间隔秒数。 |
| `indexChecksum` | 可选 index 摘要；生产者应说明是否在省略该字段的规范化内容上计算。 |
| `signature` | 第三方签名材料声明，不是安全核验。 |
| `packages` | 以包名为键的 packageEntry 映射。 |
| `_meta` | 命名空间扩展。 |

每个 `packageEntry` 包含 `latest`、`versions`、`path`，可选 `checksum`、`recordRevision` 和管理员 `subtype`。`path` 必须是安全的相对路径。

还可以包含全局包身份 `id`、最新记录的描述 `summary`、`keywords` 和生成的 `searchText`，用于直接搜索索引。相同名称可以出现在不同类型中，消费者不能用名称代替全局身份。投影优先保留 canonical index 指定的 `latest`；仅当该版本未进入当前投影时使用确定性的回退选择。`path`、摘要和搜索字段始终对应选中的版本。

## TypeScript 示例

```typescript
const index = {
  schemaVersion: 2, // 索引契约版本。
  sourceId: "agent-forge:dsh:plugin", // 源 ID。
  sourceManifest: "source.json", // manifest 相对路径。
  sourceUrl: "https://metaone01.github.io/agent-forge/data/dsh/plugin/source.json", // manifest URL。
  agentId: "dsh", // Agent 投影。
  type: "plugin", // 类型投影。
  revision: "20261001T000000Z", // 与 manifest 相同的 revision。
  generatedAt: "2026-10-01T00:00:00Z", // 生成时间。
  updatedAt: "2026-09-30T23:45:00Z", // 数据更新时间。
  ttl: 28800, // 建议八小时刷新。
  indexChecksum: { sha256: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" }, // 规范化 index 摘要声明。
  signature: { url: "https://example.com/index.sig", type: "sigstore", keyId: "catalog-key", keyUrl: "https://example.com/catalog-key" }, // 签名材料。
  packages: { // 包名映射。
    "example-skin": { // source-local 包键。
      latest: "1.4.0", // 最新版本。
      versions: ["1.3.0", "1.4.0"], // 所有索引版本。
      path: "packages/example-skin/1.4.0.json", // 详情相对路径。
      checksum: { sha256: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc" }, // 详情摘要。
      recordRevision: "20261001T000000Z", // 详情 revision。
      subtype: "skin", // 管理员 subtype。
    },
  },
  _meta: { "org.example/index": { shard: "dsh-plugin-00" } }, // 扩展。
} as const;
```
