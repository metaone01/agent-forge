# 发行候选来源名称

原来的 `git`、`registry` 来自 schema 对分发方式的分类：前者表示仓库来源，后者表示注册表来源。目录界面直接显示 `type`，因此遗漏了具体平台身份。

本轮已按确认清单改写全部安装源的类型和显示名称。GitHub 仓库为 `github-repo` / `github repository`，npm 包为 `npm-pkg` / `npm package`；PyPI、NuGet、crates.io、镜像平台、MCPB 文件和远程 MCP 端点使用下表的具体标识。GitHub 插件的 `pluginDetails.sourceType` 同步设为 `github-repo`。注册表身份、包 ID、发行候选 ID、版本和下载地址均保留。现有目录界面优先显示保存的 `name`，没有添加界面名称转换层。

Schema 已增加具体来源类型；消费者应分别按仓库/`ref`、注册表/`version`、OCI 镜像、MCPB 文件和远程服务语义处理。仍只识别旧枚举的客户端需同步更新，这项消费者适配不在当前仓库中执行。

GitHub Release 上的 MCPB 下载文件使用 `github-mcpb`，与 `github-repo` 仓库来源分开。未明确平台的 18 条镜像使用 `oci-image`，保留原声明，不猜测平台。

| 来源 | 发行候选次数 | 涉及独立包 | 当前来源类型 | 显示名称 |
| --- | ---: | ---: | --- | --- |
| GitHub 仓库 | 20,736 | 19,612 | `github-repo` | `github repository` |
| npmjs | 16,934 | 15,489 | `npm-pkg` | `npm package` |
| PyPI | 4,661 | 4,116 | `pypi-pkg` | `pypi package` |
| NuGet | 151 | 134 | `nuget-pkg` | `nuget package` |
| Cargo / crates.io | 80 | 65 | `crates-pkg` | `crates.io rust crate` |
| OCI 容器镜像 | 1,130 | 1,019 | 按平台细分，见下表 | 按平台细分 |
| MCPB 文件 | 1,582 | 970 | `github-mcpb` / `gitlab-mcpb` | 按平台细分 |
| 远程 MCP 端点 | 24,346 | 23,744 | `mcp-endpoint` | `remote mcp endpoint` |

次数按全部版本记录中的 `distributions` 计数。同一个包可以具有多种来源或多个版本，各行的独立包数不能相加作为总包数。这是元数据来源统计，没有进行网络可用性或安装验证。

OCI 来源包含 GitHub Container Registry (`ghcr.io`)、Docker Hub (`docker.io`、`hub.docker.com`)、Quay (`quay.io`) 和 Google Artifact Registry (`us-central1-docker.pkg.dev`)。少数上游记录只提供短镜像标识，尚不足以准确区分平台；如要按平台命名，应先规范这些记录。

MCPB 文件目前来自 GitHub Release、GitHub Release Assets 和 GitLab。MCPB 是文件格式，不能因此将包归类为 Agent Forge 的 `bundle`。

## 已采用的来源标识

以下命名已直接写入规范数据。标识与显示名称分别表示机器可读的来源种类和读者看到的文字；记录 ID 继续保持稳定。此次追加迁移修改了 30,067 条版本记录中的 31,950 个发行候选。

| 其它来源 | 类型标识 | 显示名称 | 发行候选次数 |
| --- | --- | --- | ---: |
| PyPI | `pypi-pkg` | `pypi package` | 4,661 |
| NuGet | `nuget-pkg` | `nuget package` | 151 |
| crates.io | `crates-pkg` | `crates.io rust crate` | 80 |
| GitHub Container Registry | `ghcr-image` | `github container image` | 813 |
| Docker Hub | `dockerhub-image` | `docker hub image` | 293 |
| Quay | `quay-image` | `quay container image` | 4 |
| Google Artifact Registry | `gcp-artifact-image` | `google artifact registry image` | 2 |
| 未明确平台的 OCI 镜像 | `oci-image` | `oci container image` | 18 |
| MCPB 文件（GitHub 地址，含 Release Assets） | `github-mcpb` | `github mcpb package` | 1,580 |
| MCPB 文件（GitLab 地址） | `gitlab-mcpb` | `gitlab mcpb package` | 2 |
| 远程 MCP 服务地址 | `mcp-endpoint` | `remote mcp endpoint` | 24,346 |

远程 MCP 端点由不同服务提供者托管，不能一律标成某个注册表平台；可在名称旁单独显示域名和 transport（`sse`、`streamable-http`）。短镜像标识和 MCPB 地址归属只反映保存的元数据，具体平台标注应核对 URL 和上游声明。

“投影”是从规范数据按 `targets` 和工具类型生成的只读发布目录，例如 `data/dsh/mcp/`、`data/claude-code/plugin/`，每个目录包含索引和详情。它不改变工具一级分类或包 ID，消费者只需读取自己关心的目录。

完整主机、数量、示例地址和数据表示见[机器可读清单](research/2026-10-02-import/distribution-sources.json)。使用 `uv run tools/distribution_names.py` 预览，增加 `--apply` 可写入本地规范数据。
