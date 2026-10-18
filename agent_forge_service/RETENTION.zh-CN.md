# 快照保留、退役游标与有界清理

本功能只管理运行目录内的不可变公开query快照。软件0.1.0、state/query首版格式1、canonical Schema v2保持；没有增加开发中间迁移，也不修改指标、first_seen或排序事实。

## 保留规则

默认游标窗口为快照退役后24小时，不是游标签发后的固定倒计时。当前revision即使长期没有更新仍继续可读，正常展示stale；切换前先持久保护新旧revision，随后才替换current.json。导入但未启用的快照从登记时起获得同样保护。重新启用一个不可变revision会重新开放其服务窗口，绑定相符的旧游标也可以继续使用；查询/filter绑定规则不变。

--snapshot-cursor-ttl-seconds可在build、snapshot-import、snapshot-activate或调度命令中设置，范围60–604800秒，默认86400。查询端读取已持久的protectedUntil，不靠自己的默认参数猜测。较短的新参数不能缩短已有保护，活跃revision始终受保护。

退役窗口结束后，即使数据库因为最近N份保留策略仍在，旧cursor也返回cursor_expired/410，显式旧revision返回revision_unavailable/410。过期检查先于HTTP缓存和304，缓存max-age也不能跨过退役期限；仍有效的旧响应可继续复用ETag。

清理默认至少保留最近3份以及当前revision；不是强制最多3份。还须等到protectedUntil后额外60秒的读请求收尾窗口，并检查活跃读取。正在使用、登记不明、校验失败或内容异常的目录保留，不为了磁盘上限删除不确定数据。若长期被读者占用或存在未知目录，实际保留数量会更多，应查看非零退出和pending原因。

生命周期记录位于snapshots/.retention.json，只有revision、文件hash与保留时间，没有插件质量权重、凭据或个人标签。该记录不进入三文件bundle；接收端按本地实际登记/启用时间创建自己的保护记录。损坏的记录阻止旧版本读取和清理，但已验证的当前revision仍可查询。没有登记时间的目录不推断期限、不自动回收。

## 先生成计划，再显式应用

```powershell
python -m agent_forge_service.worker retention-plan --runtime ./snapshots/public --keep-last 3 --max-remove 3 --output ./cleanup-plan.json
python -m agent_forge_service.worker retention-apply --runtime ./snapshots/public --plan ./cleanup-plan.json --expected-plan-sha256 PLAN_SHA256_FROM_RECEIPT
```

用第一条回执的planSha256替换第二条占位值，计划文件必须是catalog/runtime之外的新文件。计划只读取快照，不创建缺失runtime、不删除数据。计划有效10分钟，绑定runtime、current指针与保留记录hash、具体候选和文件hash。应用时在单写者锁内重新计算资格；切换、回滚、元数据变化、目标内容变化、跨目录或过期计划均不能沿用旧授权。

每次最多删除1–20个目录，默认3个；完整内容校验最多检查maxRemove的4倍候选，另有目录清单和登记表大小上限。dueCount是按时间/保留规则到期的数量，只有candidates中列出的已验证文件才进入应用；inspection_budget表示尚未检查，不等于验证成功。

应用先把候选原子移到snapshots/.trash/<revision>--<随机标识>，再仅删除计划中验证的catalog.db和manifest.json，最后删除空目录；没有递归删除未知内容。发生中断或部分文件删除失败时，新的计划会识别隔离目录中剩余的、与原登记hash相符的文件，继续处理。异常子项、链接、额外文件或未登记隔离目录保留供检查。

当前指针不被清理修改，private state/canonical/其他目录不在目标范围。只读连接在已有DB文件上持有读取租约：Windows使用打开句柄阻止重命名；POSIX使用共享flock并由清理申请独占锁。API不创建可写锁文件。存在读者、文件系统错误或其他失败时，complete=false、pending非空、CLI退出3；持续失败不会伪装成成功。POSIX真实主机行为尚未在当前Windows环境验证。

## 可选自动调用

自动入口保留全部指针、hash、时间与租约检查，应用后只打印有界摘要，避免周期日志包含整个目录清单。与发布者争用单写者锁时明确失败，可由下一周期重试；不会退款、重采、重建或重放已经完成的采集任务。

deploy/query-service/agent-forge-retention.service与.timer是未安装、未启用的systemd模板。它按小时调用显式--apply的一次性任务，不引入常驻服务或新依赖。运行者须先审查计划/参数，再自行决定是否启用；本计划没有启动定时任务或执行外部部署。

示例将写者设为agent-forge-publisher、共享读组agent-forge-query，public目录及快照归该写者/读组管理，目录0750、文件0640，任务UMask=0027；API用户只有读权限和只读文件系统。确保发布/import也使用同样的共享读组与umask，避免原子替换后新指针或.retention.json变成API不可读。私有state、gh配置和secrets保持在单独受限目录，不授予查询组。
