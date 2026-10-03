# GitHub Pages 增强：本地交付与验收结果

日期：2026-10-03（Asia/Shanghai）
状态：本地实现与验收完成；main 控制分支集成、真实 Issue/PR 与线上 Pages 验收尚未执行。

本轮使用三个 subagent 并行处理共享体验、文档构建和控制流程辅助，主工作完成上传双入口、数据投影、集成修复与最终验收。没有安装新依赖、修改 canonical 包记录、提交、推送或发布。只读 fetch 更新了远端引用；既有工作区改动和临时目录均保留。

## 1. 需求完成情况

| 需求 | 本地实现与验证 |
| --- | --- |
| README / Schema 富文本 | 12 页中英文静态文档，区分项目概览与 Schema 参考；表格、章节目录、锚点、代码复制、JSON/TS 高亮与窄屏滚动 |
| i18n | 默认中文，不随浏览器语言切换；English 显式选择持久保存；目录、详情、两种 Dashboard、文档及上传共用偏好，跨页与通用 docs 入口保持语言 |
| 主题 | 默认跟随系统；可选亮色/暗色，响应系统变化；CSS 前初始化，存储不可用有回退；主题切换实际颜色断言与截图复核 |
| 包名称与 ID | name 为主标题，canonical ID 为副标题；内部缓存键、路由与全局身份独立，Unicode 和长文本可读 |
| 胶囊标签 | facets → customFacets → keywords；颜色与辅助样式区分类别；空集合不显示；卡片前 8 个及剩余数量，详情完整展示 |
| 人类上传 | 根据真实 Schema 可视化填写五种类型，自动生成 JSON、字段错误定位、可选高级字段、复制/下载、草稿恢复，前往 GitHub 最终确认 |
| Agent JSON | 独立粘贴/导入入口、机器提交契约与完整示例；合法扩展无损保留，重复 JSON key 拒绝，不要求操作人类表单 |
| Notes | 页面、Issue Form 和离线解析都支持缺失或为空，不属于 package JSON |

文档语言切换对结构相同的中英文参考映射章节锚点；结构不一致时不猜测对应关系。Raw HTML 不执行，Mermaid 保留为代码，具体 Markdown 支持范围见构建器说明。

## 2. 数据与契约

- package Schema 仍为 v2，没有新增 tags 字段或后端。
- index Schema 新增可选 facets/customFacets/updatedAt，投影从选中的最新版本复制，避免逐卡请求详情。
- 旧索引仍能通过新 Schema；使用旧版且拒绝未知字段的严格校验器需要同步新 index Schema。这不是对所有旧消费者的前向兼容保证。
- 新可视化草稿自动记录真实 metadata createdAt，不伪造上游 publishedAt。
- main-only Issue patch 对三个时间戳全缺的 Agent 记录补真实 issue.created_at 为记录 createdAt；用户已有字段原样保留，避免合法无日期记录永远被默认投影排除。
- 浏览器检查当前打包 Schema 所用关键字，未知契约关键字 fail closed；不宣称通用 Draft 2020-12 实现。
- 新提交 CI 使用显式 --submission-formats 检查确定性 URI/date-time；随后 legacy --all 保留原行为。历史 URI 模板等格式债务未擅自迁移。
- URL 传递的 7,500 字符预算是保守应用策略，不是 GitHub 上限保证；长记录明确回退复制完整 JSON，不截断。

## 3. 最终验证证据

| 验证 | 结果 |
| --- | --- |
| Python 完整回归 | 80/80 通过，无失败或跳过；包含真实 Edge 文档测试 |
| Node 完整回归 | 52/52 通过，无失败或跳过；包含目录 UI、上传及实际生成目录的 Edge 验收 |
| 全量 canonical 校验 | 通过；3425 个警告，3423 个未列入 index 的记录，2 个 _meta 超出建议大小 |
| 真实投影生成 | 60858 条包版本投影、6 个源；轻量目录包含 57475 项 |
| 静态站点装配 | 12 页双语文档、真实 schemas、submit/契约/示例和投影数据完整 |
| main-only patch | 对核对的 origin/main 独立副本 git apply --check 通过；YAML/shell、离线 stage/publisher 与小样本管线 smoke 通过 |
| 语法与空白 | 所有站点 JS 与 Issue helper 语法通过；tracked diff 及新增源码文件 whitespace 检查通过 |
| 元数据保留 | sources 无工作区 diff；没有改写历史记录、版本、身份或发行地址 |

浏览器覆盖：默认中文/系统主题、显式偏好、系统主题变化、存储拒绝、搜索分页保留、详情往返、实际子路径、桌面和 360/390px 窄屏、文档复制与章节切换、五种类型填写、Notes 缺失、扩展字段往返、无效/重复 JSON、可选数组错误定位、草稿恢复、文件导入、长记录回退与即时主题颜色。

本地日志（在 Git 忽略的构建目录）：

- pages-staging/enhancements-evidence/python-tests.log
- pages-staging/enhancements-evidence/node-tests.log
- pages-staging/enhancements-evidence/canonical-validation.log
- pages-staging/enhancements-evidence/legacy-format-audit.log（对历史全量格式强化的诊断，不作为本轮发布门槛）

完整预览位于 pages-staging/enhancements。最终截图位于本轮 Codex visualizations 目录，包含目录亮/暗与窄屏、文档表格/代码，以及上传页。

## 4. 已修复的集成问题

- 详情初始 hash 和迟到请求的竞态；返回目录与切换语言不重新下载全部索引。
- 新页面不再依赖 page 类型硬编码站点根，实际项目子路径已验收。
- English 状态下进入文档不再意外回到中文；显式文档语言 URL 保留其语言语义。
- 可选高级数据折叠展示，字段错误会展开所在分组并聚焦；深层合法扩展保留并指引 JSON 编辑。
- JSON 解析拒绝重复 key，避免导入时静默覆盖。
- 主页面卡片原全属性过渡会在切换主题时短暂混用旧背景和新文字；限定为边框、阴影与位移过渡后，即时颜色及截图验收通过。
- 文档构建预检全部配对输入，缺失文件时不写出半套产物；不递归删除目录，拒绝不安全重叠、symlink/junction 和源/输出冲突。

## 5. main 控制分支与线上边界

当前 checkout 是 packages 来源分支，root .github/workflows 仍只保留既有 validate/project。没有把 issue-to-pr/release 加入数据分支。

控制流程改动在 docs/pages-control-plane.patch；其配套说明为 docs/PAGES-CONTROL-PLANE-HANDOFF.zh-CN.md。核对基线：origin/main=1e52acfa252dd79981271d1206484bd69fa4dfeb，origin/packages=de79474883ae202be22cdcb239ca7b1261cef698。

下一步是在获准的 main 工作区整合共享源码、文档、schemas、helper 与 patch，再审核提交。main-only 发布装配读取 main 的站点/文档/必要工具与 packages 的 canonical 数据，并保留现有部署 gating。

未验证或未执行：真实 GitHub Form 预填和登录确认、App token 权限、远端 Issue/PR、Merge Queue、artifact 上传和线上 Pages。浏览器测试阻止 GitHub 请求；离线流程没有实际远端 mutation。

真正的 Issue/PR 创建、提交、推送和发布需要主人另行授权；本地通过不等于线上已更新。历史索引/元数据大小警告和 URI 模板治理应独立处理，不用降低检查或自动迁移来掩盖。

## 6. 控件视觉修订（2026-10-03）

按 review 将主题文字选择改为太阳、月亮、显示器三个图标，保留悬停提示与本地化无障碍名称，支持方向键和当前项标记。语言改为国旗加语言名称：简体中文配中国旗、English 配美国旗；国旗为内嵌 SVG，不依赖系统 Emoji 字体。移除控件前的可见语言/主题文字，保留默认中文、默认跟随系统及已有持久化键。

共享控件覆盖目录、详情、Dashboard、文档与上传页。新增检验图标无可见主题文字、国旗 SVG、菜单展开/关闭、方向键/Escape、焦点返回、跨页偏好和窄屏菜单边界。Node 52/52 与 Python 80/80 回归通过；截图已更新。未提交、推送或发布。

## 7. main 集成与交付准备（2026-10-03）

主人已授权提交、推送和发布。在独立 codex/pages-enhancements-20261003 worktree 将上述源码与控制流程整合到 origin/main 基线，没有切换、重置原有 packages 工作区，也未修改 sources。同步现有 pyproject/uv.lock 和 identity 工具，确保 main 中测试依赖可复现；validate 增加 Node 契约检查，project 增加实际站点/文档构建。

main 集成回归：56 项 Python、51 项 Node（含本地 Edge UI/上传验证）通过，main 自身仅含控制平面空源，校验与 12 页站点装配通过。真实 packages 的 sources 与此前全量验证的本地 source tree 一致；其已生成站点另通过真实目录预览测试。main 的 Unicode 说明链接指向实际拥有该文档的 packages 分支，避免移动文档后产生 404。

分支规则要求 Pull Request、validate/project 成功及一人批准。实际推送、审批、合并和 Pages 部署结果以 GitHub Pull Request、Actions 和最后的远端/线上核验为准；本文记录交付准备，不预先宣称已部署。
