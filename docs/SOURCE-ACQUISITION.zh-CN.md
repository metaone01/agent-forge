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

## 验证状态

源码单元与 fixture Edge 浏览器主路径已通过：图标与预览独立、徽章过滤、候选加入、手动覆盖、重复读取、请求期间编辑、取消、限流、草稿恢复、英文、桌面暗色与窄屏亮色。真实 GitHub API 探针收到 rate-limit 拒绝；证明该次 browser 可读取 API 错误，不代表真实仓库全链路成功。未绕过限流或加入 token。

最终 Python 完整回归 114 项全部通过，无跳过。既有文档剪贴板测试初次读取为空：增加 Page.bringToFront 与有界等待精确内容，保留原有精确比较断言后全回归通过。JavaScript 共 85 项，83 项通过，2 个旧的可选 Playwright 测试未启用；来源提交页面、媒体与真实表单主路径由已有 Edge/CDP 实际验收覆盖。站点包含完整静态资源和 12 份双语文档。

没有新增批量 canonical 回填；原 130 条已授权媒体应用保持不变。未 commit、push 或部署 Pages。发布仍先交付 media 契约与控制面，再交付数据。

## 主人账号认证探针（2026-10-04）

使用本机 GitHub CLI 已登录的 metaone01 账号，通过 acquire 的 fetcher 接口复用同一 Pages 采集器；没有读取、输出、保存 token，也没有向浏览器注入凭据。报告位于 .collection-cache/source-authenticated-20261004.json。最终四个真实样本均成功：octocat/Hello-World 基础元数据；metaone01/agent-forge 的 site/submit/example.json 完整声明；0928OYX/dsh-free-skins 的 skin-gallery 子目录 manifest（0.1.0）；该仓库根 README（12 张 previews、3 张候选、0 icon）。观察来源固定到真实 commit，图片请求为 0。初轮一次仓库请求遇到 network 错误，复查及全探针重跑成功。

此为本地认证 API 与采集器验收，不是公开 Pages 匿名请求或认证 browser CORS 验收；匿名 API 的限流边界保持不变。未修改 Pages 凭据策略、canonical 数据或远端。

## 根 README 范围修正（2026-10-04）

修正前的账号探针是历史证据，不代表本次根 README 修正已完成真实 API 复测。新增回归覆盖根目录、子目录、明确 JSON manifest、.claude-plugin manifest、无扩展名根 README、根缺失但存在子目录 README、API 返回非根 README、部分请求失败，以及 Skill 定位不读取 Skill 图片；Edge 实际验收覆盖子目录 manifest + 根 README。本轮 Python 114 项全部通过，无跳过；JavaScript 94 项中 92 项通过，2 个可选 Playwright 测试跳过，Edge 实际验收已通过。此前两次权限审批超时后，已于 2026-10-04 22:30（Asia/Shanghai）使用 metaone01 账号成功重试，四个真实样本全部通过。skin-gallery 子目录 manifest 保留名称与 0.1.0 版本，同时与仓库根路径取得完全相同的 12 张 previews 和 3 张候选，0 icon；根 README 为 README.md，commit 为 60bed2182f79e7a62d89d770d5461e3da981547b。18 次只读 API 请求中没有子目录 README 请求，图片请求为 0；未读取或保存 token，也未向浏览器注入凭据或修改远端。此结果不代表公开 Pages 匿名限流问题已消失。

## 一致性修复与发布验收（2026-10-04）

- 自动字段归属和 provenance 随草稿保存；恢复不发请求，再次读取仍能刷新自动字段。既有同值手动字段不会误归为自动字段。provenance 每次更新，只声明实际采用的字段；用户手动维护的 provenance 不覆盖。
- 多个主要 manifest 时停止填写，提示指定 JSON 路径；不会默认取第一个。相同来源读取进行中重复点击合并为同一次请求；完成后仍可显式刷新。
- 已有采集草稿遇到部分 README / manifest 读取失败时保持原草稿，提示重试，不因不完整结果删除媒体或混合来源。根 README 缺失是明确的缺失，不转向子目录。
- JavaScript 本轮 99 项：97 项通过，2 项可选 Playwright 跳过。Edge 实际覆盖刷新版本、换源清理旧媒体、手动编辑、provenance、草稿恢复、多 manifest、重复点击和部分 API 失败。
- 认证采集抽样覆盖 200 条插件记录，189 条成功，11 条不可读取或不受支持的来源被跳过；不是全量上游采集。新增媒体 dry-run 77 条、7 个明确图标、272 张预览，0 schema 拒绝、0 合并冲突。根 README 规则与图标不使用预览兜底保持不变。
