# words.rfdsx.online 部署

生产环境使用单容器、单 Uvicorn worker 和独立 SQLite 数据卷。AI、ASR、翻译和邮件密钥只保存在服务器的 `config.json`，该文件不得提交 Git。

## 服务器目录

```text
/opt/words/
  docker-compose.prod.yml
  config.json                  # 0600，服务器私有
  data/                        # 持久 SQLite 数据及备份
  backend/ frontend/ ...       # 公共仓库工作树
```

容器加入服务器已有的 `runfuda_runfuda-internal` 网络，由 HTTPS 入口转发到 `words-app:8765`。容器不发布主机端口，资源限制为 0.5 CPU、256 MiB 内存。

仓库 `deploy/` 内提供 HTTP 跳转、HTTPS 反向代理和证书续期脚本。服务器的 Nginx 配置应在修改前备份，并在 `nginx -t` 通过后重载。Basic Auth 用户文件及 Let's Encrypt 私钥只能保留在服务器。

## 环境边界

- `WORDLEARNER_PUBLIC_MODE=1`：允许来自同机反向代理容器的请求。
- `WORDLEARNER_ALLOWED_HOSTS=words.rfdsx.online`：只接受正式域名。
- `WORDLEARNER_COOKIE_SECURE=1`：会话 Cookie 只通过 HTTPS 发送。
- Nginx 必须保留原始 `Host`、`Origin` 和 WebSocket Upgrade 头。
- 公网入口必须启用 HTTPS 和独立 Basic Auth；应用容器不得直接暴露端口。

## 更新

```bash
cd /opt/words
git pull --ff-only
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml ps
```

更新前使用 SQLite backup API 或应用自己的 `data/backups` 快照。不要直接复制正在写入的 WAL 数据库文件，也不要把正式数据库提交到 Git。

证书续期脚本 `deploy/renew-cert.sh` 使用服务器现有的 ACME webroot 和证书目录。可由 root cron 每周执行；每次续期后会先检查入口 Nginx 配置，再平滑重载。
