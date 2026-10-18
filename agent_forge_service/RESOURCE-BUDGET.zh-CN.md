# Worker 空间准入与 HTTP 条件缓存预算

本机制限制可重新获取的HTTP缓存，并在存储压力下暂停新的采集请求。历史观察、原始公开证据、first_seen、任务/预算/限流账本、来源推荐与控制记录保留，不为释放空间而删除它们，也不把缺失观察记为0。软件0.1.0、state/query首版格式1和canonical Schema v2不变，没有新迁移。

## 默认预算与配置

| 项 | 默认值 | 含义 |
| --- | ---: | --- |
| http_cache条数 | 1,024 | 所有公开提供方共享的本地可丢弃缓存数量 |
| http_cache逻辑数据 | 32 MiB | 8列全部UTF-8值字节数，含响应体、validator、键/身份和时间；不含SQLite页、索引及空闲页 |
| 缓存闲置期限 | 7天 | 从checked_at计算；过期或未来时间的缓存不发送旧validator |
| state预算 | 1 GiB | state.db、WAL、SHM实际文件字节之和；保守计入WAL重复页 |
| 文件系统余量 | 512 MiB | runtime所在盘的实际可用空间下限；不是预留配额或并发写入保证 |

参数是首版可调整的运行预算，不是容量承诺。下限不足时不会“修正”源数据或主动清空历史。

```powershell
python -m agent_forge_service.worker storage-status --runtime ./snapshots/worker
python -m agent_forge_service.worker schedule-tick --catalog . --runtime ./snapshots/worker --policy agent_forge_service/examples/scheduler-offline.json --max-requests 1 --max-batch-seconds 60 --storage-max-state-bytes 1073741824 --min-free-bytes 536870912 --cache-max-entries 1024 --cache-max-bytes 33554432 --cache-max-age-seconds 604800
```

storage-status只调用文件stat和disk_usage，不打开数据库、不创建缺失runtime、不返回credential identity或响应正文。allowed=false时退出3，原因是state_budget_exhausted或disk_reserve_low；非法参数或文件类型返回结构化错误。文件名为固定state.db/-wal/-shm，不扫描任意日志或其他目录。缺失运行目录检查最近存在的父目录所在盘，实际运行仍须有对应权限。

schedule-tick/run支持上表参数；手工采集客户端默认使用同一预算，在Python中可以通过open_state(path, resource_limits=ResourceLimits(...))显式配置。ResourceLimits/CachePolicy为标准库不可变配置，不引入服务、依赖或新状态表。运行安全上限不参与事实排名，也不改变已绑定的采集policyId参数。

## 条件缓存与事实边界

GitHub metadata/搜索、公开Git声明/Release、精选列表通过同一GitHubClient.fetch缓存路径，npm有独立来源/身份但使用相同预算函数。缓存键继续绑定来源、请求和身份，读取再核对identity；预算不会跨身份拿另一个缓存发validator。

每次成功响应写缓存的同一事务内，先按checked_at淘汰过期项，再按最近成功检查时间/稳定键排序同时满足条数和总字节预算。单个响应大于预算时本次观察仍可正常保存，但该响应不缓存；不把它当作失败或削掉来源字段。缓存与淘汰事务失败一起回滚。缓存规模内的扫描有明确条数/字节上限；不需要增加新索引或修改schema。

淘汰一条缓存也淘汰其ETag/Last-Modified；下次只发无条件GET，从成功200取得新的真实观察。304必须有仍可用的缓存，否则走现有cache_miss重试，不能拼造响应体。304只刷新checked_at与缓存留存，不推进observed_at或伪造Star增速；npm还保留原完整UTC日窗口。

collection_evidence、repository/metric/npm observations、checkpoints、jobs、rate_limits等表不被缓存预算删除。SQLite通常复用被释放的页，WAL还可能暂时增长；逻辑缓存减少不代表DB文件即时缩小。没有运行VACUUM、截断WAL或删除不可重采人工事实。

## 空间不足的行为

定时驱动每tick在打开state和规划任务之前检查空间；不足返回storage_wait、requestsThisTick=0、结构化用量/原因与下一检查时刻。已有active批次、lastTickAt和current指针不因此前移；空间或显式预算恢复后继续原驱动规则，不重置first_seen或额度。

每次GitHub/npm实际请求前再检查当前state/WAL/SHM和实际盘余量，包括继承同一客户端的匿名Git/精选通道。空间等待的CollectionError标记attempted=false，不调用transport、不计GET或增加网络attempt，不生成样本或把unknown变零。已完成/已发送的请求不因之后的空间变化倒退为未发生。

这是请求/调度的准入保护，不是硬文件系统配额：观察写入、WAL、构建、其他程序可能在检查后继续增长，真正ENOSPC仍会显式失败并按原恢复规则处理。应按实际构建峰值、快照保留和备份留出空间；不能通过降低阈值承诺磁盘永不耗尽。只读公开服务继续查询最后成功revision并展示stale；不为存储等待修改事实排序。
