# GitHub Pages 静态前端

`site/` 是 Agent Forge 的无运行时外部依赖静态应用，不保存 GitHub token，也不提供写入 API。构建器用 Python 标准库生成文档，浏览器读取本地静态 JSON。

## 页面与偏好

- 目录、详情、全局与 Agent Dashboard 共用语言和主题。
- 默认中文；可切换 English。默认跟随系统主题，可显式选亮色或暗色。
- 选择保存在浏览器；存储不可用时仍可操作。主题在 CSS 加载前初始化，语言切换保留筛选、分页及填写内容。
- `docs/` 为项目 README 与 Schema 参考，包含语法高亮、章节目录及代码复制。Markdown 正文来自根 README 和 `docs/schema/`，不是站点说明 README。
- `submit/` 提供可视化表单和独立 Agent JSON 入口；表单根据真实 `package.schema.json` 生成字段，Notes 可选。
- 包名为标题、canonical ID 为副标题；胶囊顺序为 `facets`、`customFacets`、`keywords`，空集合不显示。

## 数据契约

构建时将以下文件复制到 Pages 根目录：

- `data/manifest.json`：revision、时间、Agent 和源索引路径；源路径相对 `data/`，不是站点根目录。
- `data/<agent>/<type>/index.json`：轻量索引；包名是索引键，`id` 是 canonical 包身份。可选 `facets`、`customFacets`、`keywords` 和条目 `updatedAt` 用于直接展示与排序，不逐卡请求详情。
- `data/<agent>/<type>/packages/**/*.json`：详情；`path` 相对当前索引目录。
- `data/dashboard.json` 与 `data/agents/<agent>/dashboard.json`：统计。
- 根目录四份 `*.schema.json`：真实机器契约，供文档下载与上传检查。
- `submit/contract.json`、`submit/example.json`：机器提交契约与完整示例。

索引不可用时页面保持可读空状态。发行地址仅供选择，不会下载或执行插件。

## 提交与校验边界

可视化表单自动生成 JSON，跳转预填 GitHub Issue Form，由用户登录并最终确认。Agent 可直接提交 JSON；两者共享同一记录格式和稳定的 `Canonical package JSON` 标记。

新表单自动填写真实元数据草稿 `createdAt`，不伪造上游 `publishedAt`。Agent JSON 若三个生命周期时间戳均缺失，main 提交流程使用真实 GitHub Issue 创建时间补充记录 `createdAt`；原有字段不改写，避免无日期记录永远被默认投影排除。

浏览器字段校验器只支持当前随站点打包的 Schema 所用关键字，遇到未支持的契约扩展会拒绝提交而不是静默跳过。完整 JSON 导入保留扩展，重复对象键会被拒绝。CI 校验仍是最终门槛。

URL 传递使用保守的 7,500 字符预算，不是 GitHub 限制的保证；长记录明确回退为复制 JSON 到 GitHub 表单，不截断。Notes 不属于 package JSON。填写草稿保留在此浏览器，请勿输入秘密。

新提交的 CI 可使用 `tools/validate.py <record.json> --submission-formats` 启用与前端一致的确定性 URI/date-time 检查；历史全量校验保留原行为，旧记录中的 URI 模板债务不在本轮擅自清理。

## 本地构建与预览

在仓库根目录执行；页面、文档、Schema 和数据必须在同一个静态站点根下：

```sh
uv run tools/project.py --output data --base-url http://localhost:8000/data
uv run python tools/build_site.py --output pages-staging --data data
uv run python -m http.server 8000 --bind 127.0.0.1 --directory pages-staging
```

访问目录 `/`、文档 `/docs/` 和上传 `/submit/`。不要直接打开 HTML 文件，也不要只服务 `site/`；那样缺少构建文档、根 Schema 或投影数据。

`build_site.py` 增量装配，不递归删除目录，拒绝源/输出重叠及链接路径。数据输入必须与输出分离。Markdown 支持范围见其 `--help`；Mermaid 等未执行语言保留为代码，Raw HTML 不执行。

控制流程调整仅作为针对 `main` 的补丁交付，见 `docs/PAGES-CONTROL-PLANE-HANDOFF.zh-CN.md`。当前数据分支不增加控制工作流；真实 Issue/PR 和 Pages 发布需单独授权及验收。

## 可选展示媒体

索引可携带 `media` 摘要；skin 卡片显示首张预览，完整静态画廊读取版本记录。外链 HTTPS 图片默认不加载，用户在当前页面会话明确允许后才请求；使用懒加载、no-referrer 和固定失败占位，不代理图片、不执行插件。Schema 驱动的提交表单支持 `media.icon`、`media.previews`，JSON 导入与 Issue 交接保留原字段。发布媒体数据前先升级严格 v2 验证器；部署与回填说明见仓库中的 `docs/MEDIA-MIGRATION.zh-CN.md`。

## 来源自动填写

上传页支持公开 GitHub 仓库（可指定分支和子目录）和允许 CORS 的 HTTPS JSON。输入结束后读取，也可手动重试／取消。GitHub 图片相对路径固定到实际 commit；manifest 与明确标注 icon/logo 的 README 图片独立提供图标，截图不会补成图标。README 图片始终只从仓库根 README 提取；子目录 / manifest 路径仅定位元数据，根 README 缺失时不向子目录兜底。README 截图自动加入 previews，徽章过滤，其他图片候选需手动选择。

既有手动字段、已清空或正在手动编辑的字段优先；未修改的自动字段支持刷新和换源清理，来源记录同步更新。多 manifest 要求明确路径，部分读取失败保留已有采集草稿；读取结果与问题列在面板中。草稿恢复不会重新发送外部请求。没有声明的 ID、版本和 Agent 兼容性不猜测。只读取 JSON／README，不执行代码，不下载图片，不接收 token。限流、CORS 和超时会明确提示。

采集工具 `tools/acquire_source.cjs` 复用页面模块，默认输出只读 JSON 报告；使用 `--record-path` 可生成供 `tools/backfill_media.py --observations` 审阅的 JSONL，不直接写 canonical 数据。详细边界见 `docs/SOURCE-ACQUISITION.zh-CN.md`。
