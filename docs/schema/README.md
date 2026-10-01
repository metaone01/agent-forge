# Agent Forge Schema 文档

本目录是四份公共 JSON Schema 的协作入口。真正的机器可读契约仍然是仓库根目录的 `*.schema.json`；这里解释字段含义、约束边界和完整 TypeScript 风格示例。

## 文件

| Schema | 用途 | 说明 |
| --- | --- | --- |
| [package.schema.md](package.schema.md) | 包版本记录 | Agent、类型、兼容范围、facet、Bundle、发行候选 |
| [source.schema.md](source.schema.md) | 元数据源 manifest | 一个 Agent/type 目录及其元数据镜像 |
| [index.schema.md](index.schema.md) | 包索引 | 低带宽搜索和版本发现 |
| [advisory.schema.md](advisory.schema.md) | Advisory 快照 | 第三方通告的整合记录，不是安全检测结果 |

对应的英文文档使用 `_en` 后缀，例如 `package.schema_en.md`。

## 两层地址模型

`source.schema.json` 和 `index.schema.json` 描述 Agent Forge 元数据源。`sourceMirrors`、`mirrorOf`、`revision` 和 `priority` 用于在 GitHub Pages、地区镜像或其他只读站点之间切换。

`package.schema.json` 的 `distributions` 描述插件或工具本体的发行候选，例如 Git 仓库、Release、Archive、Registry 或 OCI。它们不是 Agent Forge 元数据源。安装器可以选择发行候选，但 Agent Forge 不下载、不代理、不验证，也不保证候选内容。

## 参与开发时的建议

机器可读 Schema 是最终权威；Schema README 是最适合人类查字段的入口；TypeScript 注释示例最适合快速理解对象层级和填写方式。三者一起能显著降低新贡献者误填字段的概率，但任何示例都不能替代 validator 和跨文件规则。

所有内容只表达元数据整合，不表示来源、checksum、signature、facet、兼容范围或 advisory 已经被安全核验。
