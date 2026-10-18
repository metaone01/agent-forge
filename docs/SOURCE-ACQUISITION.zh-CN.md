# 来源自动读取与手动编辑（2026-10-04）

## 已实现的行为

- Pages 上传页提供包类型、来源 URL、可选 ref/path。输入完成触发读取，也可重试／取消；恢复草稿不重新读取。
- 支持公开 GitHub 仓库和支持 CORS 的 HTTPS .json；带 slash 的 branch 使用显式 ref，tree/blob 的歧义不会猜测。
- 读取 repository 与真实 commit，再列举有限 manifest 文件：plugin.json、dsh-plugin.json、manifest.json、package.json、.claude-plugin/plugin.json、.dsh/plugin.json；MCP 可读取 server.json。不会递归遍历或运行代码。
- 标准 v2 JSON 保留声明字段；普通 manifest 映射名称、版本、描述、许可证、链接、关键词及合法的作者、平台、标签、type-specific 声明。缺少 ID、version、Agent targets 时不凭空推断。
- 图标来自明确 icon/iconUrl/media.icon 或 README 明确标为 icon/logo/图标的图片；previews 不填充 icon。
- README 图片始终只从仓库根 README 提取，与子目录 / manifest 路径无关；路径仅用于定位包元数据，不接受 README 路径；SKILL.md 仍可定位 Skill，但不提取其中的图片。通过固定 commit 的根 /readme 接口读取，也支持根 README 无扩展名。根 README 缺失时报告 readme-missing，不向子目录兜底；请求失败仍保留可用 manifest 字段。
- README 支持常见 Markdown 图片、引用式图片及 HTML img。截图／预览标签自动加入 previews；badge、代码段、注释排除；其他图片在候选面板手动添加。此实现不是完整 CommonMark parser；复杂模板、picture/srcset、SVG 内嵌及不受支持语法不会擅自补全。
- 相对图片路径固定到对应仓库实际 commit；原有绝对 HTTPS 地址不改写。媒体仅是未验证声明，页面读取时不下载图片。
- 已有手动数据与用户修改／清空／移除的字段优先，包括读取期间修改。仅跟踪本采集器实际填入的字段；未修改的自动字段可刷新，换源时先移除旧自动字段，再采用新来源，避免默认混源。图标与预览数组作为完整媒体声明保护。读取报告保留来源和问题；候选添加与媒体编辑进入同一份 schema-driven JSON、草稿和 Issue 交接。

## 边界

每个响应与 README 上限 1 MiB，读取超时 25 秒；预览最多 12 张，README 候选最多 100 张。无 token、cookie 或 Referer，不跟随重定向；拒绝 query、fragment、凭据 URL、危险协议、重复 JSON keys。只读取元数据，不执行脚本、渲染上游 HTML、下载图片或提交远端。GitHub API 限流／CORS／部分 README 失败会明确显示。

Python 社区导入器仅当 ref 明确属于记录所在仓库且 path 就是 manifest/skill path 时，使用其 commit 解析相对媒体；目录仓库的 revision 不能冒充资源仓库的 revision。

## 同一采集器的 CLI

```powershell
node tools/acquire_source.cjs --source https://github.com/author/skin --type plugin
node tools/acquire_source.cjs --source https://github.com/author/skin --type plugin --ref main --path plugin.json --record-path sources/plugin/packages/example/1.json
```

默认输出只读报告。--record-path 输出可供 backfill_media.py --observations 审阅的 JSONL；有问题和候选时另写 stderr，不直接应用 canonical。CLI 位于 main 控制面，依赖同仓库 site/assets/source-metadata.js、submission-core.js 和 package.schema.json；部署时不要单独复制 CLI。
