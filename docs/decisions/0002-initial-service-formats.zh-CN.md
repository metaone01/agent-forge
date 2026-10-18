# 首版服务格式与版本规则

软件版本保持0.1.0，不按功能步骤、测试轮次或打包次数递增。不同数据契约独立编号：

| 契约 | 首版 |
| --- | --- |
| Worker state SQLite | user_version=1，storageContract=agent-forge-worker-state |
| 查询SQLite/manifest | 格式1，storageContract=agent-forge-query-snapshot |
| 批次策略、公开bundle、私有backup、离线格式 | 各自格式1，以kind及独立契约区分 |
| 原规范package/source/index JSON | 保持Schema v2 |

数据revision是快照标识，不是软件版本。policyId标识固定配置，参数变化需新ID，不能重用旧ID改变含义。

开发中间格式不作为支持发布版本，不提供逐级转换或仅重写user_version的“迁移”。新安装在新runtime初始化当前格式；已有数据库必须匹配版本与storageContract，拒绝发生在业务写入前。当前格式的备份/恢复、旧revision、游标和显式回滚继续受支持。

未来真实发布后，必要的不兼容变化需先做一致性备份和源/目标格式验证，再执行受控事务迁移；成功后才提升版本，失败可恢复。公开查询快照优先从可信state/canonical重建，不对在线数据库就地变更。未发生的未来迁移不提前增加实现。
