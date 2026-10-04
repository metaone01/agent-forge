# 展示媒体迁移：图标与静态预览

## 契约

此扩展保持 `schemaVersion: 2` 和已有 `$id`。所有媒体字段可选，旧记录不需要补图。skin 仍是 `type=plugin`、`subtype=skin`；没有新增一级类型。

```json
{
  "media": {
    "icon": { "url": "https://example.org/icon.png", "alt": "Example Skin 图标" },
    "previews": [
      { "url": "https://example.org/dark.png", "alt": "深色对话页面", "theme": "dark" },
      { "url": "https://example.org/light.png", "alt": "浅色对话页面", "theme": "light" }
    ]
  }
}
```

- `media` 至少包含 `icon` 或 `previews`；不接受任意额外字段。
- `icon` 要求 `url` 和 `alt`；`previews` 有序，1–12 项，每项要求 `url` 和 `alt`。
- URL 最长 4096 字符，必须是无用户名/密码的绝对 HTTPS 引用；`alt` 是 1–500 字符的非空白文本。
- 预览可选 `theme=light|dark|system`，仅是图片的声明，不是 Agent 或运行时兼容性。
- 第一张预览是默认缩略图。索引 `packageEntry.media` 只复制所选版本的图标和至多一张预览；完整画廊必须读取其 `path` 指向的记录。
- 媒体地址和内容未经核验；不下载、不缓存、不代理图片，不运行插件生成截图，不加载 HTML、iframe 或交互式皮肤代码。
- SQLite 快照继续作为搜索/记录路径索引，不新增媒体列；完整记录仍是媒体权威来源。

## 兼容与部署顺序

这不是“旧验证器也接受新记录”的承诺。旧 schema 的 `additionalProperties: false` 会拒绝新增 `media`，旧索引验证器同样会拒绝媒体摘要。

1. 先发布并升级控制面、`packages` 分支的校验/投影工具，以及所有严格验证 package/index 的消费者。
2. 更新提交页面与需要展示媒体的客户端。能忽略未知可选字段的宽松消费者不必同步新增 UI；严格 schema 消费者未升级前，不向其发布新字段。
3. 生成并审阅小批量回填报告和补丁，检查原始 SHA-256、来源 revision、冲突和待解析地址。
4. 获得数据修改许可后，在独立数据 PR 中应用已审阅的记录。刷新变更记录的 `updatedAt`，重新生成 canonical index（保留选定 latest）、相关 source/index revision 与已有 checksum；不能把补丁目录直接当完整 source tree 发布。
5. 执行全量校验、投影、快照及站点构建。投影器重新计算 package 和 index checksum，不能沿用旧值。
6. 完成真实浏览器验收后，再按原有 `main` 控制面 / `packages` 数据职责发布。保留原记录和报告以便回滚。

不能协调升级的消费者，可暂存 namespaced `_meta`，但它没有公共展示语义；不建立永久双写协议。若新旧契约必须长期独立，另行评估 v3。

## 导入规则

`tools/media.py` 只提取已声明的 `media.icon`、`media.previews`、`icon`、`iconUrl`、`preview`、`previews`、`screenshots`。同一观察内以标准 `media` 优先，其次按上述旧字段顺序处理。数组保持顺序，重复 URL 不重复追加。

没有文字描述的旧字段，以上游名称生成中性的 `alt`，不推断图片内容。已有图标和同 URL 的描述/主题优先；冲突保留原值并进入报告，预览最多 12 张。

绝对 HTTPS 地址原样保留，包括上游明确给出的 `HEAD` 地址；它不是不可变资产，不能把目录仓库的 commit 冒充插件仓库的 commit。相对地址默认待处理，只有显式 `assetContext` 指明图片所属仓库、固定 commit 和元数据文件位置时才解析。不得猜测图片路径或使用仓库头像替代插件图标。

接受的资源及其观察来源记录到 `_meta["org.agentforge/media-provenance"].sources`。v1 迁移额外把不能映射的媒体字段记入迁移信息；旧 `_meta["org.agentforge/media"]` 保留不删除，回填可将其中明确声明的资源提升到标准 `media`。

## 本地 dry-run

使用现有环境，不需要新增依赖：

```powershell
.venv/Scripts/python.exe tools/backfill_media.py --report .collection-cache/media-backfill-report.json
.venv/Scripts/python.exe tools/backfill_media.py --report .collection-cache/media-review-report.json --patch-dir .collection-cache/media-review
```

默认只扫描插件；可用 `--type` 选择其他类型。工具不会写 `sources/`，也没有隐式 apply 或网络请求。补丁目录必须是新目录，报告不能覆盖规范记录、schema 或非回填报告文件。

本地缓存的 metadata 必须通过 `catalog-fetch.json` 的仓库、revision、文件路径匹配，并按原 collection 的 entry 定位。不能匹配的来源计入 `unmatchedCachedSources`，不使用其他 revision 猜测回填；无结构化媒体字段的缓存不从 README 推断图片。

如需提供经过确认的额外观察，可用 `--observations observations.jsonl`。每行示例：

```json
{"recordPath":"sources/plugin/packages/example/1.json","source":{"repository":"author/skin","revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","path":"assets/plugin.json"},"metadata":{"name":"Example Skin","preview":"preview.png"},"assetContext":{"repository":"author/skin","revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","path":"assets/plugin.json"}}
```

`recordPath` 必须指向所选类型的现有规范记录；`source` 是观察依据，`assetContext` 是明确的图片解析上下文，两者不能混淆。报告列出新增图标、预览、仅来源变化、schema 拒绝、未匹配缓存和冲突；审阅补丁保存完整拟议记录和原文件 SHA-256。重复观察不会继续追加资源或来源。

## 受控本地应用

生成器 `backfill_media.py` 仍只读；经过授权的应用使用独立 `tools/apply_media.py`，默认也是只读预检：

```powershell
.venv/Scripts/python.exe tools/apply_media.py --review .collection-cache/media-review-20261003 --revision media-20261004
.venv/Scripts/python.exe tools/apply_media.py --review .collection-cache/media-review-20261003 --revision media-20261004 --apply --backup-dir .collection-cache/media-backup-20261004
```

应用前统一检查 plan 所属 root/type、数量、冲突、原文件 SHA-256、schema、身份及字段边界。提案只能增补媒体和媒体来源，不能修改其他字段、覆盖既有媒体或删除来源。应用时刷新记录的 `updatedAt`，保留 canonical index 的 latest、版本列表及路径，更新所选版本的媒体摘要和时间。source/index 使用新 revision 与生成时间；已有 package checksum 会重新计算，已有 recordRevision 会与新 index revision 对齐。

只有原本声明的 checksum 才会更新：source 的 indexChecksum 覆盖最终 index 字节；index 自身的 indexChecksum 覆盖移除该字段后、UTF-8／两空格缩进／末尾换行的 JSON 字节。未声明 checksum 的 canonical 文件不会凭空增加 checksum，投影器仍会为生成资产计算 checksum。

写入前将所有原文件按原始字节保存到新的独立备份目录，生成 `application.json`（每个文件的原／新 SHA-256、应用时间及 revision）。使用逐文件原子替换；捕获到写入错误时恢复已经写入的文件，发现并发修改则拒绝覆盖并标记 `rollback-incomplete`。这不是跨文件事务：进程被强制终止时可能部分应用，必须用备份报告核对当前 hash 后人工恢复，不得直接覆盖新的并发修改。

本地应用不会升级远程消费者，也不会提交、推送或发布。发布门禁仍是先交付契约／工具，再交付数据。

## Pages 验收

外链图片默认不加载。用户选择“加载外链图片”后，本次页面会话才请求 HTTPS 图片；不持久保存此选择。图片请求设置 `referrerpolicy=no-referrer`，但仍会把网络请求发送给图片站点。隐藏图片只停止当前展示，不撤销已经发生的请求或清除浏览器缓存。

列表展示图标，skin 另显示首张预览；详情展示完整有序画廊。固定尺寸占位、懒加载、无图回退、失效图片回退必须在桌面/移动端和明暗主题下检查。非法 URL 不进入图片 `src`，所有替代文本均按文本转义，不注入 markup。
