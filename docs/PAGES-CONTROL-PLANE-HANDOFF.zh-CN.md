# Pages control plane：Issue submission 与 main workflow 交接

更新：2026-10-03。本文交付的是离线 helper、测试和 **main-only patch**，不是已经启用的 GitHub 自动化。

## 1. 范围与基线

本控制流程子任务只新增以下四个文件，不直接修改现有 workflow、Issue Form、canonical records 或站点：

- `tools/issue_submission.cjs`
- `tests/issue-submission.test.cjs`
- `docs/pages-control-plane.patch`
- `docs/PAGES-CONTROL-PLANE-HANDOFF.zh-CN.md`

patch 基于本地已 fetch 并重新核对的引用：

- `origin/main`: `1e52acfa252dd79981271d1206484bd69fa4dfeb`
- `origin/packages`: `de79474883ae202be22cdcb239ca7b1261cef698`

当前共享 checkout 是 packages 来源分支。**不要把 issue-to-pr/release 放进当前 root `.github/workflows`**。patch 只供主工作在获准的 main 工作区整合；不包含提交、推送、真实 Issue 或远端资源操作。

patch 涉及 main 上四个既有文件：`issue-to-pr.yml`、`release.yml`、`ISSUE_TEMPLATE/package-submission.yml` 与 `ISSUE_TEMPLATE/config.yml`（均位于 `.github`）。模板/config 是当前共享目录本轮版本相对于上述 main 基线的完整增量，包含视觉入口、Agent/JSON 说明、禁止提交 secrets 和 Notes 可选；不是只改 required。

## 2. 上传页 ↔ Issue ↔ Agent 的固定协议

- Issue 模板查询：`template=package-submission.yml`。
- 必填 textarea id：`package_json`，值是 **一个完整 Schema v2 package JSON**，不是只有 identity 的片段。
- 可选 textarea id：`rationale`；为空时可以不放入 URL。
- 模板固定 label：`package-submission`。
- GitHub 渲染正文必须有精确 heading：`### Canonical package JSON`。
- 可选正文 section 为 `### Submission notes`；helper 也支持 `### Notes`。
- `package_json` 支持纯文本 JSON，或自成行的 untagged/`json` fenced JSON；可以使用更长 backtick fence 或 tilde fence。
- Agent 的备用模式是 **整个 Issue body 就是一个完整 JSON object**，同时仍须带固定 label 才会触发 workflow。不能在 raw JSON 后附加 Notes 或第二个 JSON。
- 可视化页只生成预填链接，仍由用户在 GitHub 确认提交。不要在页面或 helper 中直接创建 Issue。

URL 构造示意（浏览器内）：

```js
const url = new URL('https://github.com/metaone01/agent-forge/issues/new');
url.searchParams.set('template', 'package-submission.yml');
url.searchParams.set('labels', 'package-submission');
url.searchParams.set('package_json', JSON.stringify(record, null, 2));
if (rationale.trim()) url.searchParams.set('rationale', rationale.trim());
// URLSearchParams 编码完整文本；不要先 encodeURIComponent 再二次编码。
```

预填 URL 有浏览器/平台长度限制，需由上传页保留复制 JSON 的 Agent/manual fallback；本 helper 不负责 UI 或 GitHub URL 长度策略。

## 3. helper API 与校验边界

CommonJS，可直接由 `actions/github-script` 从 main control checkout `require`：

```js
const { parseIssue, prepareRecord, packagePath, TARGET_BRANCH } = require(helperAbsolutePath);
const { record: submitted, notes } = parseIssue(issue.body || '');
const record = prepareRecord(submitted, issue.created_at);
const relative = packagePath(record);
// TARGET_BRANCH === 'packages'
```

`parseIssue(body, { validateCanonical })` 返回 `{ record, notes }`。Notes 缺失、空白或 `_No response_` 都转为 `''`。JSON 中任何原始字段都不被改写，尤其不 slugify/normalize `name`、`id` 或 `version`。

可注入 **同步** canonical 校验函数，参数为 record：返回 `true`/`undefined` 表示成功；返回 `false`、其他结果或 throw 表示失败；Promise 不被当作校验成功。适合将既有 canonical 校验器接入离线调用，不附带 JS schema validator，不添加依赖。生产 patch 仍以 Python CLI 作完整校验。

扫描器按字符串、转义与对象/数组深度确定 JSON 边界，再由 JSON.parse 校验语法。JSON 字符串中的 `###`、backticks、花括号不会截断 payload。fence 必须完整配对并自成行；Notes 内 fenced heading 不作为第二次提交。重复 JSON key（含转义等价 key）、重复 canonical section、追加 JSON/正文、损坏 fence 都明确拒绝。

identity guard 要求 schemaVersion=2、五类 type 和合法的 id/name/version 字符串，拒绝空白、控制字符、未配对 surrogate、非法 id 空白/backslash 和 id/name 超长；version 防御性要求非全空白且无控制字符。它 **不是** description/targets/distributions/type-specific details、URI/date 或 catalog 跨记录校验的替代品。

错误含清晰 message 和 code：`BODY`、`SECTION`、`JSON`、`FENCE`、`IDENTITY`、`VALIDATION`、`PATH`、`TIMESTAMP`。CI 失败直接在对应 step 显示，不在校验失败时额外创建 Issue comment/branch/PR。

### createdAt：catalog recordcreation，而非上游发布时间

Schema 的 createdAt/updatedAt/publishedAt 是可选字段，但 projection 默认 cutoff 不发布无时间记录。因此仅靠 schema 合法性不足以保证新 Issue 能进入 catalog。

prepareRecord(record, issueCreatedAt) 在 **createdAt、updatedAt、publishedAt 三个 own fields 全部不存在** 时，返回浅拷贝并新增 createdAt=issue.created_at。该值表示 GitHub submission 对应的 catalog metadata record 创建时间，不声称上游在这一时间发布；不会补 publishedAt，也不改任何已有原字段。可视化表单自己的 createdAt 若已存在，则原样保留；Agent raw JSON 若无时间也不会永久隐藏。

有任一用户时间字段时，record 原样返回；即使用户字段是 null/空串/非法日期，也不静默修补，后续 submission canonical gate 会拒绝。GitHub creation timestamp 本身必须是有效 RFC 3339/calendar date/time/offset；缺失或非法时 fail closed（TIMESTAMP），不能用 Date.parse 的日期自动纠错或 Date.now 替代。

stage 与 publisher 都从同一个 event 的 issue.created_at 确定性调用 prepareRecord，并对准备后的 JSON 作完整比较。cutoff 和历史数据语义不变：issue 创建后仍须达到正常十分钟 cutoff；不强行立即发布、不替历史无日期包补时间。

### 路径与 Python identity/projection 对照

输出布局：

```text
sources/{type}/packages/{encoded-name-prefix}--{sha256(name)[:12]}/{encoded-version-prefix}--{sha256(version)[:12]}.json
```

- encoding 与 `tools/identity.py:encoded_component` 一致：UTF-8 bytes，保留字母数字及 `@._+-`，其余用大写 `~HH`（不是文件名里的 `%HH`）。slash/backslash/percent/tilde/Unicode 均保留在 component 内，不形成目录穿越；一次 HTTP URL decode 不改变 tilde escapes。
- 未截断的 encoding 完全可逆；name/version 的哈希分别区别大小写、不同上游字符串与相同可读前缀。逻辑身份遵循 Python 的 `(type, name)` 和 `(type, name, version)`，不在文件名里额外拼 id；id 冲突交由 `identity_errors` 检查。
- name/version prefix 上限分别为 110/64 encoded ASCII bytes，与 projection 的预算和 12 位 SHA-256 suffix 对齐。
- **长 Unicode 的有意差别**：Python 当前 projection 用直接字符串切片；helper 不截断一个 UTF-8 字符或 `~HH` token。因此未截断的路径一致，长值的 prefix 可能稍短。截断路径不能恢复完整原值，完整身份必须读 JSON，不能把可读前缀当作 name；canonical 路径不要求与生成的 projection 路径完全相同。
- 12 位 hash 不宣称数学上无碰撞。暂存使用 `wx`，已有同一路径明确失败而不是静默覆盖；全量 identity 校验还能发现其他旧路径上的逻辑重复。没有自动更新/删除旧 record 的行为。

## 4. main Issue control flow

patch 中顺序固定为：

1. 先 checkout `main` 到 `control`，再 checkout **packages** 到 `canonical`，均不持久化 credentials。
2. setup-uv；仅在 runner 本地将 main `tools/validate.py`、`tools/identity.py` 和 package/index/source/advisory 四份 schemas 复制到 canonical checkout。
3. require control helper，从 Issue event body 解析完整 record，调用 prepareRecord 并补齐必要的 recordcreation 时间，再在 canonical `sources/{type}/packages` 下暂存。逐层拒绝 symlink/非目录；已有目标文件不覆盖。
4. 在 canonical 工作区执行：

   ```bash
   uv run --with jsonschema --with referencing python tools/validate.py "$record_path" --submission-formats
   uv run --with jsonschema --with referencing python tools/validate.py --all
   ```

   `validate.py` 的 ROOT 由其脚本路径决定，所以把工具与匹配 schemas 放到实际 canonical checkout；只设置 working-directory 而从 control 执行 validate.py 是错误的。
5. 两次校验成功后才申请 GitHub App token、查询 `heads/packages`，检查其 SHA 与已经校验的本地 packages HEAD 一致。
6. SHA 已前进则失败并要求 rerun；否则从校验 SHA 创建 proposal branch，只上传 candidate JSON，然后以 **base=packages** 建 PR、保留 labels/cooldown/Merge Queue 说明。

不会把本地覆盖的 validator/schema 文件一起提交到 proposal。label gate、短期 App token 与最终 packages PR/merge-group validate/project checks 保留；preflight 不能替代最终 checks。SHA 检查之后仍可能有 target 并发前进，最终 Merge Queue 校验负责新合并上下文。

同一个 Issue 的 workflow 串行运行。`proposal/issue-N` 已存在时不会覆盖或删除分支；API 后半段失败可能留下 proposal branch，需 maintainer 在检查后人工处理，不能通过自动清理删除远端资源。

### Legacy format debt 与新 submission gate

历史 metadata 中存在 placeholder URI 模板地址等格式债务。这轮 **不修改历史包记录，不对全 catalog 新增 deterministic strict formats**。

- `--submission-formats` 仅为单个新 record 启用标准库 URI/date guards；负例包括无效 calendar date、缺少 time/offset、相对 URI 与不合法 placeholder URI。
- 后续 `--all` 和 release 的 `--all` 不带该 flag，保留既有 `FormatChecker()` 行为和跨文件/identity 校验。
- `--all --submission-formats` 是非法组合，CLI 明确报错；不是把历史失败静默过滤掉。
- 默认 FormatChecker 的已有 optional-dependency 环境差异没有在本 patch 中重构。未来历史 format debt 治理需要单独审计、迁移授权与变更设计。

## 5. release：main 内容、packages 数据，选定 overlay

该 main 基线原本只 checkout packages，并直接 cp site/data。本 patch 在 packages checkout 后增加 main control checkout，保留 additive main site 覆写，并扩展到以下 **必要文件**：

- `control/site/.` → `site/`；保留 packages-only 文件，main 对同路径有最终优先级。
- `README.md` / `README_en.md` → checkout root。
- `docs/schema/.` → `docs/schema/`，包括双语 schema 文档。
- package/index/source/advisory 四个 `*.schema.json` → root。
- `tools/build_site.py`、`tools/project.py`、`tools/validate.py`、`tools/identity.py` → tools。

不复制 collectors、整个 tools、sources 或全 repo；canonical records 始终来自 packages。snapshot.py 保留原 checkout 的现有逻辑，没有另加新的 snapshot/collector 依赖。

随后保持既有 canonical legacy 校验、十分钟 cutoff、tar 和 SQLite artifact 流程，将站点装配替换为：

```bash
uv run python tools/build_site.py --output pages-staging --data data --root .
```

这保证 assembler 读到的是 main README/双语文档、schemas、site 和当前 data，而 projection 使用能输出 index `facets`、`customFacets`、`updatedAt` 的新版 project/schema pair；接口 schemaVersion 仍是 2。

changed 检测除既有 packages diff 外，比较最近 catalog release notes 中的 `Control-plane commit: SHA`。main-only 内容变化不会被 packages 没变而吞掉；旧 release 没有该标记时保守发布一次。任何 main commit 都可能触发一次 release，刻意不另加文件级兼容状态。标记写入后续 immutable release notes。

**原有 gating 不变**：release 只在 `changed == 'true'` 时创建；Pages 在 changed 或手动 workflow_dispatch 时部署。保留 github-pages environment、artifact 路径和部署 action。

### Schema/data 匹配风险

main 工具与四个 schemas 必须作为一个匹配集合先交付。SchemaVersion 仍为 2 不代表所有历史 metadata 自动兼容新的额外约束；如果 main schema 与 packages 数据不兼容，Issue preflight 的 --all 或 release validate 会失败。应停止发布并定位差异，不能删除 required checks、降低新 submission 格式测试，或擅自改包记录来获得通过。

本 patch 保持旧 PR checks，不替主人修改 packages 工作流。主工作还应确认最终 PR/Merge Queue 采用的 canonical contract 与 main submission gate 相容，避免两个分支工具版本永久分叉。

## 6. 离线验证证据

本次验证没有安装依赖，也没有修改 Git index/HEAD、真实 branch/PR/Issue 或部署环境。

- `node --test tests/issue-submission.test.cjs`：**26/26 通过**；node:test/标准库，无 npm dependencies。
- `node --check tools/issue_submission.cjs`：通过。
- 测试包括 Unicode/路径穿越、大小写/slug/truncated-prefix 冲突、Notes optional、raw JSON、fence/heading 字符串、损坏/重复 JSON、注入校验失败，新增无时间补 createdAt、已有任一时间原样保留、GitHub 非法 creation time failclosed、stage/publish 确定性一致测试，并执行 patch 中的 stage/publish JS（GitHub/FS mocks）验证失败前零远端 mutation、stale SHA、固定 packages base。
- 现有 PyYAML 和 Git Bash 检查实际 proposed workflow YAML、每个 run block shell syntax；比较确认 release/pages gating 与 environment 未改。
- 在独立目录从 origin/packages 导出 validator/schemas 起点，再执行 patch 的真实 Issue overlay 命令，使用当前 main-bound 工作文件运行 Python 单 record 新格式 gate 和 legacy --all；验证缺失 targets、相对/placeholder URI、无效 date、全局 id 冲突、重复 package/version 都按预期失败。
- 在同一隔离小样本执行 patch 的 release overlay，再运行真实 legacy --all → project → 新 index schema validation → SQLite snapshots → build_site。检查 index 的 facets/customFacets/updatedAt、schemaVersion=2 和中英 docs、submit、schemas、data 输出均存在且匹配。
- encoding 与 Python identity/projection 的未截断案例做了直接 parity 比较；另用真实 stage JS/Python gate/project/publisher mocks 跑无日期 Agent submission，确认 createdAt 补齐后在正常 cutoff 之后进入 projection。
- `git apply --check` **对 origin/main 四份原始文件的非 Git 独立目录通过**，不是对当前 packages worktree apply。

本机验证目录（不是部署目录）：

```text
C:\Users\metaone\.codex\visualizations\2026\10\03\01a100cc-8d55-7502-bc82-c6df4b13b65a\pages-control-plane-check-uRGisN\main-base
C:\Users\metaone\.codex\visualizations\2026\10\03\01a100cc-8d55-7502-bc82-c6df4b13b65a\pages-control-plane-check-uRGisN\main-proposed
C:\Users\metaone\.codex\visualizations\2026\10\03\01a100cc-8d55-7502-bc82-c6df4b13b65a\pages-control-plane-check-uRGisN\pipeline-smoke-slhf_c_c
```

apply check 命令：

```powershell
git -C 'C:\Users\metaone\.codex\visualizations\2026\10\03\01a100cc-8d55-7502-bc82-c6df4b13b65a\pages-control-plane-check-uRGisN\main-base' apply --check 'G:\Code\sourcerepo\agent-forge\docs\pages-control-plane.patch'
```

**验证边界**：small fixture smoke 使用当前共享目录的 main-bound 文件，不代表这些文件已经进入 origin/main；未扫描全 60,868 个 tracked sources 文件。运行的是已有 Python 环境中的实际脚本，不是联网 uv runner；没有实际 App-token 权限、Issue 表单提交、PR/Merge Queue、release artifact upload 或 Pages visual/live deployment 验收。

## 7. 待主工作执行的集成点

1. 在获准 main 工作区交付 helper/tests，以及最新 validator（包含 --submission-formats）、identity、project、匹配 schemas、build_site、双语 README/schema docs、site 上传页与 assets。该基线 main 缺若干工具/文档；不能只 apply workflow 就启用。
2. 校对 root Issue Form/config 是否在并行工作期间又改动；若 origin/main 已更新，重新生成/验证 patch，不盲套旧 hunks。
3. 在 main 工作区按正常审核流程应用 patch；保留 packages root 仅 validate/project 的职责。
4. 用最终整合后的完整 packages 数据跑 legacy --all、projection/index schema、snapshot、build_site；如遇 schema/data mismatch，报告并停止，不自动迁移记录。
5. 在受控测试仓库/获准流程中验证 GitHub Form 预填/Notes empty、App permissions、invalid submission 不建 branch、valid submission 只提 packages PR、packages 并发前进及重复执行错误。
6. 分别验收 main-only site/doc/project 变化的 changed 检测、scheduled/dispatch deploy gating、生成站点的上传与双语文档链接。真实发布、推送和 Issue/PR 创建仍须主人授权。
