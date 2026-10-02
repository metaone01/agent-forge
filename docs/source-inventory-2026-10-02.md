# 数据源盘点与数量快照

盘点日期：2026-10-02（Asia/Shanghai）。本轮范围：寻找来源、确认读取入口、统计候选数据规模并留档。状态：来源调查，等待主人决定是否继续收录。

本轮没有向 `sources/`、`data/`、发布索引或任何 Agent 投影写入新包，没有安装或执行上游内容。这里的数量不代表有效、可安装、兼容、可信或安全；也没有核验第三方所称的 verified、approved 等状态。

## 计数口径

- **文件枚举**：固定 Git revision，在完整、未截断的目录树中统计匹配的 YAML、`SKILL.md` 或 manifest 文件；一个文件算一条候选。
- **集合枚举**：读取固定 revision 的 JSON 内部数组、以仓库身份为键的字典或 CSV 数据行。JSON 外层的字段数不是条目数；CSV 表头不计。
- **链接枚举**：从指定 Markdown 文件的列表或表格提取 GitHub 仓库链接，文件内按仓库身份去重。它是发现规模的代理值，可能包含教程、目录和核心项目，也可能漏掉外部网站或引用式链接，不作为精确包数。
- **API 报告**：只记录平台返回的 `total_count` / `total`，没有完整下载搜索结果。GitHub 的普通搜索每次查询最多可取前 1,000 条，不能据此声称已取得所有匹配仓库。
- **Registry 分页**：请求 `version=latest`，沿 `nextCursor` 翻页到结束，统计返回记录和按原始大小写区分的唯一 server name；对 API 返回的所有状态均保留，不做有效性筛选或身份规范化。分页期间上游可能改变，因此这是连续读取快照，不是原子数据库快照。
- **未计数**：请求失败或集合结构尚未确定时明确保留未知，不以 0 替代。

同一项目可能出现在多个目录、多个 topic、npm 与 Registry 中；一个仓库也可能包含多个插件或 Skill。本文件不将这些数量相加为独立包总数。精选集、候选集、站点生成文件和中英文 README 常是同一数据的不同视图，不能重复计算。

## 已调查来源

### DSH 社区目录、Skill、主题与预设

| 来源 | 建议归属 | 实际枚举规模 | 入口与口径 | 注意事项 |
| --- | --- | --- | --- | --- |
| [awesome-dsh-plugin/awesome-dsh-plugin](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin) | `plugin` 为主，混合类型待拆 | 4,412 个 YAML | `data/plugins/*.yml` 文件；[文件树](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin/tree/bb8496ec4cbb9217b33bf081ec4cdf06b51501e8) | 一条目录记录未必对应一个独立仓库或插件。 |
| [beancookie/awesome-dsh-plugin](https://github.com/beancookie/awesome-dsh-plugin) | 混合 `plugin` / `skill` / `general` | 503 条 | `plugins[]`；[docs/plugins.json](https://github.com/beancookie/awesome-dsh-plugin/blob/8f779f6938b5f3434729be589c82bcd7db4c32f7/docs/plugins.json) | 网站与 README 是相同来源的视图。 |
| [bruc3van/awesome-dsh-plugin](https://github.com/bruc3van/awesome-dsh-plugin) | 混合类型 | 17,028 条仓库；522 个仓库身份的安装资料 | `repositories[]`；另见 `data/packages.json.entries`；[data/repositories.json](https://github.com/bruc3van/awesome-dsh-plugin/blob/d562ead4b6b62cc8ebaf77f469335dfd6b659897/data/repositories.json) | 522 是安装资料字典的仓库键数，不能当作所有独立 package 数；两集合不相加。 |
| [dshworks/awesome-dsh-plugins](https://github.com/dshworks/awesome-dsh-plugins) | 混合 `plugin` / `skill` / `bundle` 候选 | 17,323 条主集合；55 条候选 | `plugins[]`；另见 `data/candidates.json.candidates[]`；[data/plugins.json](https://github.com/dshworks/awesome-dsh-plugins/blob/6c211eaaab7e74e7cb3e5c04110ec1db88510074/data/plugins.json) | 保留上游分类和状态，不采用其有效性结论。 |
| [cccakeee/awesome-dsh-plugins](https://github.com/cccakeee/awesome-dsh-plugins) | 混合类型 | 2,866 行聚合清单；2,126 行上游 verified 清单 | CSV 数据行；另见 `data/verified-plugins.csv`；[data/repositories.csv](https://github.com/cccakeee/awesome-dsh-plugins/blob/9a568253bdc74c36f7017bc4c14ce21b1b690dc5/data/repositories.csv) | 分别记录两个视图，未核验 verified 声明；README 的 2,072 声明与实读 2,126 行不一致。 |
| [kejixiaoliang/awesome-dsh-plugins](https://github.com/kejixiaoliang/awesome-dsh-plugins) | 混合类型 | 353 个仓库链接 | Markdown 文件内去重；[INDEX.md](https://github.com/kejixiaoliang/awesome-dsh-plugins/blob/b38612ffc864e695a565112252acc6e3e01ced19/INDEX.md) | 是候选链接数；其中可能有核心仓库、目录和桌面工具。 |
| [awesome-deepseekharness/awesome-deepseek-harness](https://github.com/awesome-deepseekharness/awesome-deepseek-harness) | 混合类型 | 135 个仓库链接 | Markdown 文件内去重；[README.md](https://github.com/awesome-deepseekharness/awesome-deepseek-harness/blob/f231506b84d13cea2a7d90476c4936c6300d6ef3/README.md) | 还包含基础设施、学习与社区链接。 |
| [white0dew/awesome-dsh-plugins](https://github.com/white0dew/awesome-dsh-plugins) | 混合类型 | 8,522 条 catalog；11,708 条 topic 快照 | `plugins[]`；另见 `github-topic-dsh-plugin.json.records[]`；[data/sources/github-plugin-catalog.json](https://github.com/white0dew/awesome-dsh-plugins/blob/55ece09ae6278782389c3cc64691ec8797b26623/data/sources/github-plugin-catalog.json) | 这两个集合与其他社区源可能重叠，不能直接相加。 |
| [wgd753/awesome-dsh-plugin](https://github.com/wgd753/awesome-dsh-plugin) | 混合类型 | 7,242 个仓库键 | 以 `owner/repo` 为键的 JSON 字典；[data/repositories.json](https://github.com/wgd753/awesome-dsh-plugin/blob/274ef577d5c5ab8194a782e0ac5adc1585bc8737/data/repositories.json) | 发现池包含核心项目和周边项目，需逐条确定是否在收录范围。 |
| [fjzzwxp/awesome-dsh-plugins](https://github.com/fjzzwxp/awesome-dsh-plugins) | 混合类型 | 603 个 YAML | `data/plugins/*.yml` 文件；[文件树](https://github.com/fjzzwxp/awesome-dsh-plugins/tree/c4210499ca17689e4ea99dcff87071c5bc87cd69) | 相似目录文件结构不代表与其他来源相互独立。 |
| [hackerFish/awesome-dsh-skills](https://github.com/hackerFish/awesome-dsh-skills) | `skill` | 19 个 `SKILL.md` | 完整文件树；[文件树](https://github.com/hackerFish/awesome-dsh-skills/tree/d982b4a04c87ea8b0a254ebae33d923e9d4a351a) | 文件存在不代表已验证 Skill 可用性。 |
| [yzfly/awesome-dsh-skills](https://github.com/yzfly/awesome-dsh-skills) | `skill` 发现池 | 995 条 skills；995 条 candidates；3 个本仓库 Skill 文件 | JSON 数组；另见 `data/candidates.json`；[data/skills.json](https://github.com/yzfly/awesome-dsh-skills/blob/4b7d4cfc8a5287e6443c381a3d0fe61b639c6892/data/skills.json) | 995 条来源记录不是 995 个本仓库 Skill 文件；三种口径不相加。 |
| [zhiwehu/awesome_dsh_skills](https://github.com/zhiwehu/awesome_dsh_skills) | `skill` | 1 个 `SKILL.md` | 完整文件树；[文件树](https://github.com/zhiwehu/awesome_dsh_skills/tree/15a13d953460f8c4955cdb9191dde61f42aa8457) | 当前文件数量有限，不以仓库名称推断规模。 |
| [hackerFish/awesome-dsh-presets](https://github.com/hackerFish/awesome-dsh-presets) | `bundle` 候选 / 配置待判 | 4 个 preset manifest | `presets/*/preset.yml`；[文件树](https://github.com/hackerFish/awesome-dsh-presets/tree/35151e000478550a0ef0c80af1680af607bbe390) | 需要判断组合成员能否对应规范包身份。 |
| [dataelement/awesome-dsh-workbench](https://github.com/dataelement/awesome-dsh-workbench) | `plugin` / `bundle` / `general` 待判 | 10 个 workbench YAML | `data/workbenches/*.yml`；[文件树](https://github.com/dataelement/awesome-dsh-workbench/tree/786b775374fc4d145d5a90e4ceda4e7b31862bab) | 工作台不是现有一级分类，应按具体发行结构映射。 |
| [dshworks/awesome-dsh-themes](https://github.com/dshworks/awesome-dsh-themes) | `plugin`，主题/skin 候选 | 556 条主题；26 条候选 | `themes[]`；另见 `data/candidates.json.candidates[]`；[data/themes.json](https://github.com/dshworks/awesome-dsh-themes/blob/4cd8edf3ee3aad99dec2cdd41c29ec656cb62da0/data/themes.json) | 主题目录与普通插件目录可能交叉收录。 |
| [Renakoni/awesome-dsh-themes](https://github.com/Renakoni/awesome-dsh-themes) | `plugin`，主题/skin 候选 | 132 个 theme YAML；JSON 同为 132 条 | `themes[]` 与 `entries/*/theme.yml`；[data/catalog.json](https://github.com/Renakoni/awesome-dsh-themes/blob/21f142d019238667381cedfcb608cc0b7652a7b6/data/catalog.json) | 两个视图的数量一致，但不能因此宣称内容或兼容性已核验。 |
| [web-casa/awesome-cordis-plugins](https://github.com/web-casa/awesome-cordis-plugins) | `plugin` 候选 | 328 条 | `plugins[]`；[data/plugins.json](https://github.com/web-casa/awesome-cordis-plugins/blob/d1c5eac8f1bb1308adfe77e077e968f7a2b93d2a/data/plugins.json) | Cordis 插件与 DSH 适用性必须分开记录。 |
| [kingselyjoe/awesome-dsh-list](https://github.com/kingselyjoe/awesome-dsh-list) | 混合类型 | 1,010 个仓库链接 | Markdown 文件内去重；[README.md](https://github.com/kingselyjoe/awesome-dsh-list/blob/d67ba17e53c45c77ed5d281df7e0cd76db746b06/README.md) | 是链接代理值，不是完整 topic 总量或独立包总量。 |
| [the-beating-light-of-the-nail/awesome-dsh-plugin-stock](https://github.com/the-beating-light-of-the-nail/awesome-dsh-plugin-stock) | 混合类型，金融领域 facet | 28 个仓库链接 | Markdown 文件内去重；[README.md](https://github.com/the-beating-light-of-the-nail/awesome-dsh-plugin-stock/blob/a4dc7661b9178023d4c8bcdb107e24fe9c35e0e9/README.md) | 可能含相关资源链接，不将 28 等同于精选插件数。 |
| [YYTbit/awesome-dsh-bridges](https://github.com/YYTbit/awesome-dsh-bridges) | `plugin` 候选 | 6 个列出的包名 | 包名列表；4 bridge + 2 utility；[README.md](https://github.com/YYTbit/awesome-dsh-bridges/blob/f26012b13edf9f59d3d2872c717b4af226aa0803/README.md) | 本轮未检查这些包名是否在 npm 存在；其 README 没有可被通用链接统计器识别的仓库条目。 |

### 跨 Agent 插件、Skill、MCP 与工具

| 来源 | 建议归属 | 实际枚举规模 | 入口与口径 | 注意事项 |
| --- | --- | --- | --- | --- |
| [anthropics/claude-plugins-official](https://github.com/anthropics/claude-plugins-official) | `plugin`；内含 `skill` 待拆 | 315 个 marketplace 条目；33 个 Skill 文件 | `plugins[]` 与完整文件树；[.claude-plugin/marketplace.json](https://github.com/anthropics/claude-plugins-official/blob/ab024cdcfa7ca80be204acd4907656ba5a968589/.claude-plugin/marketplace.json) | Claude 来源，不自动声明 DSH 兼容；两种类型计数不能相加为独立包数。 |
| [anthropics/skills](https://github.com/anthropics/skills) | `skill` | 19 个对外 skills 目录文件 | `skills/*/SKILL.md`；[文件树](https://github.com/anthropics/skills/tree/8a1541c4a3ffa5a20a5a91de0dcf3f0bab1d1ef4) | 统计发布目录；不把仓库其他位置的辅助 Skill 文件混入该数。 |
| [ComposioHQ/awesome-claude-skills](https://github.com/ComposioHQ/awesome-claude-skills) | `skill` | 864 个 `SKILL.md` | 完整文件树；[文件树](https://github.com/ComposioHQ/awesome-claude-skills/tree/be2a406907dbc61b73e6827ded415c96139d13a2) | 一仓多 Skill；后续需要分辨发布内容与辅助文件。 |
| [obra/superpowers](https://github.com/obra/superpowers) | `skill` 为主；容器 `plugin` 待判 | 15 个 Skill 文件 | `skills/*/SKILL.md`；[文件树](https://github.com/obra/superpowers/tree/8ca22dba9a94f28898bbce59f2537ff4d87c747d) | 插件入口与内部 Skill 分属不同包身份层级，后续处理。 |
| [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills) | `skill` | 25 个 `SKILL.md` | 完整文件树；[文件树](https://github.com/addyosmani/agent-skills/tree/9d0c60d406b454a78ccc0a175b19932047aa4dac) | 兼容 Agent 和发布目录边界留待后续判断。 |
| [VoltAgent/awesome-agent-skills](https://github.com/VoltAgent/awesome-agent-skills) | `skill` 发现导航 | 243 个仓库链接；19 个显式 Skill 文件链接 | 两种去重链接口径；[README.md](https://github.com/VoltAgent/awesome-agent-skills/blob/af465bbef5632ee23cf20a2291df31b29d3fd0c9/README.md) | 仓库可能包含很多 Skill；未采用页面 badge 的 1497+ 作为实枚举数量。 |
| [modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers) | `mcp` | 7 个当前 `src/*` 参考实现目录 | `src/*/README.md` 文件；[文件树](https://github.com/modelcontextprotocol/servers/tree/f46d9578190b476b3501923ea8977d899e8db2cb) | 不代表其 README 链接的所有第三方 server 都已枚举。 |
| [punkpeye/awesome-mcp-servers](https://github.com/punkpeye/awesome-mcp-servers) | `mcp` 发现导航 | 4,080 个仓库链接 | Markdown 文件内去重；[README.md](https://github.com/punkpeye/awesome-mcp-servers/blob/3c30195615095be4f22ac120a7cee82f215cf547/README.md) | 仍可能含说明或导航链接；npm、远程服务、非 GitHub 项目不一定落入该口径。 |
| [e2b-dev/awesome-ai-agents](https://github.com/e2b-dev/awesome-ai-agents) | `general` 候选 | 215 个项目标题；83 个仓库链接 | 项目章节与去重仓库链接；[README.md](https://github.com/e2b-dev/awesome-ai-agents/blob/9596ab1e69fbbb6c95291141c8dab5c181083216/README.md) | 章节包含外部网站项目；是两个视图，尚未判断哪些能作为 Agent 工具收录。 |

### Registry 与平台搜索

MCP Registry 已完整分页读取。时间区间为 **2026-10-02 11:44:44 至 12:03:01（Asia/Shanghai）**；共 **383 页、38,278 条记录、38,278 个区分大小写的原始 server name**。最后一页 78 条，`nextCursor` 为空，所有返回记录的 `isLatest` 都为 true。状态分布为 **active 37,824、deprecated 454**，没有按状态排除记录。状态分布不是本项目的有效性结论。

平台搜索只读取报告数量，没有完整枚举结果；与 Registry 的计数口径不同。

| 入口 | 当前报告数量 | 计数单位 | 收录提示 |
| --- | --- | --- | --- |
| [MCP 官方 Registry](https://registry.modelcontextprotocol.io/v0.1/servers?limit=100&version=latest) | 38,278 | latest 记录，完整 cursor 分页 | `mcp` 来源；包含返回的远程 endpoint 与 package 记录。后续仍需判断本项目能如何表示每条记录。 |
| [GitHub topic:dsh-plugin](https://api.github.com/search/repositories?q=topic:dsh-plugin&per_page=1) | 17,051 | 仓库，API 报告 | 混合发现池，包含目录、应用和带标签的其他项目。 |
| [GitHub topic:dsh-skill](https://api.github.com/search/repositories?q=topic:dsh-skill&per_page=1) | 210 | 仓库，API 报告 | 一仓可能有多 Skill，不等于 Skill 条目数。 |
| [GitHub topic:deepseek-harness](https://api.github.com/search/repositories?q=topic:deepseek-harness&per_page=1) | 13,112 | 仓库，API 报告 | 广义生态，需限制到 Agent 工具范围。 |
| [npm keywords:dsh-plugin](https://registry.npmjs.org/-/v1/search?text=keywords:dsh-plugin&size=1) | 6,573 | 搜索匹配的 npm 包，API 报告 | 保留版本与 package 身份，keyword 不是兼容性声明。 |
| [npm keywords:mcp-server](https://registry.npmjs.org/-/v1/search?text=keywords:mcp-server&size=1) | 9,320 | 搜索匹配的 npm 包，API 报告 | 可能含客户端、网关和辅助库，需判断实际服务类型。 |

共计详细调查 **30 个仓库来源、1 个 MCP Registry 和 5 个平台搜索入口**。未对跨来源身份作全局去重，因此不提供“共有多少独立可收录包”的总数。

## 分类建议与边界

以下是后续录入建议，并非本轮对每个条目的最终分类。

| 实际发布内容 | 建议类型 | 必须保留的判断边界 |
| --- | --- | --- |
| 提供 MCP 协议服务的 server 或远程 endpoint | `mcp` | npm/PyPI 是发行方式，不是一级分类；MCP 客户端或 DSH 的 MCP 适配插件不能仅凭名称归入 `mcp`。 |
| 由 DSH/Cordis/Claude 等宿主加载的扩展、主题、skin、provider 或 adapter | `plugin` | 社区的 UI、memory、workflow 等目录通常是能力分组，应保留为关键词或受控 facet，不直接增加一级类型。 |
| 可独立采用的 `SKILL.md` 与关联资源 | `skill` | 一仓多 Skill 应按实际目录身份拆分；Skill 的安装器本身通常是工具。仓库辅助开发 Skill 与对外发布 Skill 的区别留待后续判断。 |
| 服务 Agent 工作流的桌面宿主、CLI、框架、评估、沙箱或管理工具 | `general` | 需有实际 `toolType` / `agentUse` 依据；教程、导航页、普通语言包和纯网站不能机械转成工具。 |
| 明确组合多个已收录包的发行集合或预设 | `bundle` 候选 | 预设和工作台不必然是 Bundle；只有成员身份可映射到现有包时才能建 Bundle，单个配置文件可能属于其他类型或不在收录范围。 |

官方或社区来源中的 Claude/其他 Agent 数据可以进入对应 Agent 的规范记录，但不能自动投影为 DSH 兼容。兼容信息缺失时保留 `unknown`，不推断版本范围。来源标题写着 plugin、skill 或 bundle，也不能代替条目自身发布结构的判断。

## 读取方式与限制

GitHub 仓库优先读取目录树和固定 revision 的文件；本轮部分匿名 API 请求触发速率限制，随后使用本机已有 GitHub CLI 登录进行公开只读查询。没有添加 token 到脚本或证据文件。`raw.githubusercontent.com` 在本次网络环境未能正常读取，因此文件内容改由 GitHub Contents/Blobs API 取得。

README 中的数量声明可能与数据文件不同，取实际集合枚举作为主值，并明确子集关系。既有 [`collection-report.md`](collection-report.md) 是历史收录报告，其 34,802 个 MCP latest 记录、310 个 Claude marketplace 条目、925 个 Skill 等数字没有直接用于本轮当前数量。

这些研究来源不是 `source.json` 中的 Agent Forge 元数据镜像。本轮不修改 source manifest，不将第三方目录误登记为可共享 Agent Forge revision 的镜像。

## 后续选择

后续已获授权进行分类收录，结果见[收录报告](community-import-2026-10-02.md)。本文保留最初的来源调查口径，收录数量另行记录。

建议首先处理具备结构化数据的 DSH 目录，再补充原生 Skill 和主题来源；MCP Registry 可作为独立批次。GitHub topic 和 npm keyword 的匹配范围较广，适合补漏，不适合直接整批发布。

主人决定继续后，下一轮可以按选定来源提取条目、保留来源和 revision、按实际包身份去重、依据 manifest / `SKILL.md` / MCP 声明确定一级分类，并把缺失或冲突字段留为未知。最终有效性标准和收录范围应在该轮明确。

## 证据与复算

- [`catalogs.json`](research/2026-10-02-source-inventory/catalogs.json)：逐来源 revision、文件路径或集合身份、数量、计数方法、读取时间和错误。
- [`discovery.json`](research/2026-10-02-source-inventory/discovery.json)：`awesome-dsh` 搜索返回的仓库清单。搜索命中并不意味着它一定是数据源。
- [`discovered-repositories.md`](research/2026-10-02-source-inventory/discovered-repositories.md)：99 个搜索命中的可读清单，标记哪些已详细调查、哪些尚未展开。
- [`search-totals.json`](research/2026-10-02-source-inventory/search-totals.json)：GitHub topic 与 npm keyword 查询入口、平台报告数量和样本。
- [`mcp-registry.json`](research/2026-10-02-source-inventory/mcp-registry.json)：Registry 分页计数、cursor、身份、状态汇总与完成标志。
- [`measure.ps1`](research/2026-10-02-source-inventory/measure.ps1)：使用既有 PowerShell 和 GitHub CLI 的只读统计脚本；`catalogs` 模式重新读取当前仓库版本，`registry` 模式重新分页统计 Registry。复跑会更新本研究目录的快照文件，未来数量可能变化。

可选复算命令：

```powershell
pwsh -NoProfile -File docs/research/2026-10-02-source-inventory/measure.ps1 -Mode catalogs
pwsh -NoProfile -File docs/research/2026-10-02-source-inventory/measure.ps1 -Mode registry
```

本轮验证结果：30 个仓库来源均读取成功，未遇到目录树截断；文件、数组、字典键、CSV 行和 Markdown 提取结果均与保存数量一致。99 个搜索命中与保存清单一致。MCP 的逐页数量之和、身份列表长度和记录总数一致，最后 cursor 为空，且全部返回条目标记为 latest。文档和脚本通过基础格式与语法检查。本轮没有进行有效性、安装、运行或宿主兼容测试。
