# 前端API查询模式

`/catalog/`使用只读API查询完整SQLite快照；main 现有首页、Dashboard、`/api/` 查询协议和 SQLite 搜索实现保留；新增服务使用 `/api/v1/` 与 `/catalog/`，不替换现有接口。查询失败不会静默混入另一版结果。

## 接线

- 页面`agent-forge-api-base` meta配置API地址，默认相对`../api/v1/`。
- 同域部署将构建后的site作为静态根，并将`/api/`转发给查询服务。
- 跨域时同时设置页面地址及服务明确的`--allow-origin`；不发送用户token或Cookie。
- HTML需通过HTTP托管，不能以file协议直接打开；API本身不托管静态HTML。

## 查询行为

搜索、Agent/type/subtype、规范/自定义标签、来源筛选与排序先在完整快照执行，再按稳定游标分页。游标绑定revision、筛选和页大小；过期提示重新读取目录，不把旧游标跳到新数据。

详情支持含slash/Unicode的包ID，版本使用精确值；仓库观察、npm下载、来源精选和Release分别显示。没有采集的数据保留unknown，下载次数不称为使用人数，来源精选不构成质量、安全或兼容背书。

候选scope独立于规范目录，可按元数据分拣状态筛选；发现或可解析不等于正式收录。来源停用、审核与写入只通过受控本地CLI，公共API不能修改数据或触发采集。

API 页面通过 `api-presentation.js` 使用展示助手，独立于现有首页的 `app.js`；共享 main 的主题、资源与语言偏好。API 页面外部图片默认需用户选择加载；加载失败不影响搜索。慢请求和筛选切换使用取消与代次检查，避免迟到结果覆盖当前页面。

启动见[使用说明](../README-DEPLOYMENT.md)，接口参数见[HTTP契约](../agent_forge_service/HTTP-CONTRACT.zh-CN.md)。
