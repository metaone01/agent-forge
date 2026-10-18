# 固定 revision 的 Agent/type 离线元数据

`offline-export`从已发布的不可变公开查询库导出一个Agent和一个类别的规范元数据。只读取快照，不读取/创建private state，不联网，不修改canonical、current或其他快照。在线数据库仍统一存储；离线文件是派生产物，不是按Agent拆分后端。

```powershell
python -m agent_forge_service.worker offline-export --runtime /srv/agent-forge/public --revision example-revision --agent dsh --type plugin --output /srv/agent-forge/offline-dsh-plugin
python -m agent_forge_service.worker offline-verify --runtime /srv/agent-forge/public --directory /srv/agent-forge/offline-dsh-plugin --expected-export-sha256 EXPORT_SHA256_FROM_RECEIPT
```

第二条命令的`EXPORT_SHA256_FROM_RECEIPT`须替换成第一条输出的`exportSha256`。可信SHA-256应通过可信通道获得；文件自身的hash不认证来源。verify的runtime参数沿用Worker CLI形式，不会打开或创建该目录。也可以在Python中直接调用`verify_offline(directory, expected_export_sha256=...)`。

已有工具提供同一导出入口：

```powershell
python tools/snapshot.py --query-runtime /srv/agent-forge/public --revision example-revision --agent dsh --type plugin --output /srv/agent-forge/offline-dsh-plugin-via-tool
```

原`python tools/snapshot.py --data data --output snapshots`保持原静态JSON索引契约；两种模式不能混用参数。新模式输出目录必须不存在，且不能与catalog/runtime包含或重叠；失败保留不完整目录供检查，不覆盖旧导出。输出只有`offline.json`与`catalog.db`，无需服务器即可用SQLite读取；没有插件文件、脚本执行器或额外数据库服务。

## 范围与版本语义

范围策略`version-target-membership-v1`逐版本检查规范记录中显式的targets.agentId和type。保留全部符合范围的历史版本，原canonical_json、payload_hash、source_path、canonical_time逐字保留，targets也不裁剪；不由仓库/标签推断兼容性。此范围是版本声明的关联，不保证包的当前最新版本支持该Agent，计数可能不同于在线按当前选中版本过滤的数量。

packages保留源canonical_latest、selection_status、first_seen_at、bootstrap；额外selected_version/selected_status只说明该最新版本在本导出中的可用性：

| selected_status | 含义 |
| --- | --- |
| available | 源快照选中的规范版本包含在范围内，selected_version保持该版本 |
| not_in_scope | 包有符合范围的历史版本，但源规范最新版本不在范围内；selected_version为null |
| unknown | 源规范最新版本本身未知；selected_version为null |

不会用单个剩余版本、字典序或发布时间重新选“最新”。first_seen/bootstrap不重置；导出时间不冒充上游创建或发现时间。

这是独立schemaVersion=1、kind=agent-forge-offline-canonical格式，包含规范元数据和本地名称/描述FTS5索引。缺少可用选中版本时，描述索引为空，名称仍可搜索；不把旧版描述当最新版。FTS使用unicode61，中文不承诺分词或与在线trigram/相关度语义相同。下载、Star、候选、来源精选、控制/审核账本与调度状态不在此离线格式内；要保留完整公开情报和在线查询语义，应使用原三文件query-snapshot bundle。离线文件不能用snapshot-import当在线快照启用，也不支持在线cursor。

## 校验与读取

manifest记录源schemaVersion/storageContract、revision、manifest/DB SHA-256、原generatedAt和canonicalCutoff、范围策略、计数与离线DB大小/hash。数据库metadata重复绑定source/scope。verify检查精确两文件集、受信descriptor hash、DB hash/byte limit、无链接/硬链接、固定SQLite schema、完整性/外键、计数、规范Schema/身份/目标范围/payload hash、latest规则和FTS内容。verify证明导出自洽，可信hash和传输通道仍由运行者负责；它不查询远端验证原始声明。

SQLite只读示例，参数均作为值绑定：

```python
import sqlite3
from pathlib import Path
uri = Path("/srv/agent-forge/offline-dsh-plugin/catalog.db").resolve().as_uri()
db = sqlite3.connect(uri + "?mode=ro&immutable=1", uri=True)
try:
    rows = db.execute("SELECT id,name,selected_version,selected_status FROM packages ORDER BY name,id LIMIT ?", (20,)).fetchall()
    matches = db.execute("SELECT package_id,name FROM packages_fts WHERE packages_fts MATCH ? LIMIT ?", ('"terminal"', 20)).fetchall()
finally:
    db.close()
```

只对经过校验且不可变的文件使用immutable。接收时先传输到新的目录并verify；本地复制验证不代表跨主机权限、TLS或灾备验收。数据库上限2GiB，由导出SQLite页数与verify字节数共同约束。磁盘不足/中断时不会生成完成descriptor；无需清理现有数据来重跑，改用另一个新输出目录即可。
