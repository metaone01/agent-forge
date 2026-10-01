# `advisory.schema.json` 字段说明

## 用途和边界

Advisory 是第三方安全通告的快照或整合记录，不是 Agent Forge 的检测结果。`disclaimer` 必须明确：数据来自第三方，不保证准确、完整、及时或安全。Agent Forge 不扫描上游代码，也不确认通告真假。

## 字段

| 字段 | 作用 |
| --- | --- |
| `schemaVersion` | 固定为 `2`。 |
| `id` | 通告稳定 ID，例如 GHSA、CVE、OSV 或来源自定义 ID。 |
| `aliases` | 同一通告的其他 ID。 |
| `source` | 通告来源名称。 |
| `sourceUrl` | 来源页面。 |
| `sourceRevision` | 外部通告源的 revision。 |
| `asOf` | 本快照代表的时间。 |
| `status` | draft、published、withdrawn 或 superseded。 |
| `publishedAt`/`withdrawnAt` | 通告状态时间。 |
| `ttl` | 建议刷新间隔秒数。 |
| `disclaimer` | 必填免责声明。 |
| `affectedPackages` | 受影响的 Agent Forge 包定位和版本范围。 |
| `advisories` | 详细 advisory 对象列表，可含 severity、CVSS、CWE、affected 和 references。 |
| `signature` | 外部签名材料声明。 |
| `_meta` | 命名空间扩展。 |

每个 `affectedPackage` 可含 `name`、`sourceId`、`agentId`、`type`、`range`、`versionScheme`、`introduced`、`fixed` 和 `lastAffected`。不要使用旧的 `typeRef` 或 `kind`。

## TypeScript 示例

```typescript
const advisory = {
  schemaVersion: 2, // Advisory 契约版本。
  id: "GHSA-example-1234", // 稳定通告 ID。
  aliases: ["CVE-2026-1234", "OSV-2026-example"], // 别名。
  source: "Example Security Database", // 来源名称。
  sourceUrl: "https://security.example.com/advisories/GHSA-example-1234", // 来源页面。
  sourceRevision: "20261001", // 外部来源 revision。
  asOf: "2026-10-01T00:00:00Z", // 快照时间。
  status: "published", // 通告状态。
  publishedAt: "2026-09-29T00:00:00Z", // 发布时间。
  withdrawnAt: "2026-12-01T00:00:00Z", // 示例撤回时间；实际撤回前可省略。
  ttl: 86400, // 一天刷新提示。
  disclaimer: "Third-party metadata. This is not a detection result and carries no guarantee of accuracy, completeness, timeliness, or safety.", // 必须保留边界。
  affectedPackages: [{ // 受影响包。
    name: "example-skin", // 包名。
    sourceId: "agent-forge:dsh:plugin", // 来源目录。
    agentId: "dsh", // Agent。
    type: "plugin", // 五种类型之一。
    range: "<1.4.0", // 受影响范围。
    versionScheme: "semver", // 范围规则。
    introduced: "1.0.0", // 引入版本。
    fixed: "1.4.0", // 修复版本声明。
    lastAffected: "1.3.9", // 最后受影响版本。
  }],
  advisories: [{ // 详细通告。
    id: "GHSA-example-1234", // 详细 ID。
    aliases: ["CVE-2026-1234"], // 详细别名。
    severity: "high", // 来源声明的严重性。
    cvss: { version: "3.1", score: 8.1, vector: "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:N" }, // CVSS 声明。
    cwe: ["CWE-79"], // CWE 声明。
    summary: "Example advisory summary.", // 摘要。
    impact: "Example impact description.", // 影响。
    affected: { range: "<1.4.0", versionScheme: "semver", introduced: "1.0.0", fixed: "1.4.0", lastAffected: "1.3.9" }, // 详细受影响范围。
    fixedIn: "1.4.0", // 修复版本。
    references: ["https://security.example.com/advisories/GHSA-example-1234"], // 参考链接。
    publishedAt: "2026-09-29T00:00:00Z", // 详细发布时间。
    updatedAt: "2026-10-01T00:00:00Z", // 详细更新时间。
    status: "confirmed", // 来源状态。
    source: "Example Security Database", // 详细来源。
    sourceUrl: "https://security.example.com/advisories/GHSA-example-1234", // 详细来源 URL。
  }],
  signature: { url: "https://security.example.com/advisories/GHSA-example-1234.sig", type: "pgp", keyId: "advisory-key", keyUrl: "https://security.example.com/advisory-key" }, // 外部签名声明。
  _meta: { "org.example/advisory": { importedBy: "community" } }, // 扩展。
} as const;
```
