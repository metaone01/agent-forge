# GitHub Pages 静态前端

`site/` 是 Agent Forge 的零依赖 GitHub Pages 应用。它只读取生成后的静态 JSON，不保存 GitHub token，也不提供写入 API。

## 数据契约

Actions 发布时将生成文件复制到 Pages 根目录的 `data/`：

- `data/manifest.json`：当前 revision、生成时间、Agent 列表和可选的 index URL 列表。
- `data/<agent>/<type>/index.json`：一个 Agent/type 的轻量索引；`packages` 可以是对象（名称到索引项）或数组。
- `data/<agent>/<type>/packages/**/*.json`：包详情；索引项的 `path` 相对当前 index 所在目录。
- `data/dashboard.json`：全局 Dashboard 统计。
- `data/agents/<agent>/dashboard.json`：单 Agent Dashboard 统计。

索引不可用时页面保持可读的空状态；这不会将旧 revision 与新 revision 静默混合。索引中的链接和 `distributions` 只展示给用户，页面不会下载或执行插件。

## 本地查看

需要一个静态 HTTP server（直接打开 HTML 会受浏览器的 `fetch` 同源限制影响）。例如：

```sh
npx serve site
```

生产 Pages 部署由仓库 Actions 负责，前端不依赖运行时 CDN 或后端服务。
