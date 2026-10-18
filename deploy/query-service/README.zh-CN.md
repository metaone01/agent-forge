# 查询服务部署文件

本目录提供锁定HTTP依赖、公开bundle接收说明及systemd/nginx样例。模板须按目标主机路径、专用用户和域名配置；准备部署文件不等于实际部署完成。

源码使用入口见[启动说明](../../README-DEPLOYMENT.md)，服务契约见[HTTP说明](../../agent_forge_service/HTTP-CONTRACT.zh-CN.md)。

## 构建独立交付目录

在源码仓库中，从已生成的summary数据及当前格式公开bundle构建新目录：

```sh
python tools/package_query_service.py build --data data --bundle /srv/agent-forge/bundle --expected-bundle-sha256 TRUSTED_BUNDLE_SHA256 --output /srv/agent-forge/delivery
```

用实际回执的可信bundleSha256替换占位符；output必须是不存在的新目录。结果包含必要代码、Schema、站点、公开bundle、requirements及逐文件manifest，不含私有state或凭据。按生成目录的README-DEPLOYMENT.md校验、导入并显式启用。构建/校验工具不安装或部署服务。

## 主机运行

```sh
cd /opt/agent-forge
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r deploy/query-service/requirements.txt
.venv/bin/python -m agent_forge_service.preflight --catalog /opt/agent-forge --runtime /srv/agent-forge/public
```

使用[systemd模板](agent-forge-query.service)前配置专用查询用户、代码读取权限、公开runtime只读权限及独立日志目录。采集state不提供给API。进程默认仅监听loopback，`ExecStartPre`预检失败时不启动。

[nginx片段](nginx.conf.example)置于已配置TLS的server内；静态根只指向summary站点，`/api/v1/`与health转发到API。不要将代码、bundle或runtime作为静态根。示例按域名根路径编写，子路径部署需调整URI改写并验证搜索/详情。

容器配方位于`deploy/Dockerfile`和`deploy/compose.yaml`；需要明确指定经审阅的镜像digest和公开runtime路径。镜像及配置在目标环境中仍需实际构建、启动和检查。

## 更新依赖与数据

依赖变更先更新pyproject.toml和uv.lock，再生成固定哈希清单并验证：

```sh
uv export --locked --extra http --no-default-groups --no-emit-project --output-file deploy/query-service/requirements.txt
```

数据更新先校验/导入新bundle，再凭当前pointer hash显式激活。保留可用旧revision用于恢复；不就地覆盖正在查询的数据库。完整命令在独立交付的README中。
