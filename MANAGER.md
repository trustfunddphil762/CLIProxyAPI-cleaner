# MANAGER.md

`manager.sh` 是这个仓库里**推荐的 systemd 安装方式**。

如果你准备把 `CLIProxyAPI-cleaner` 跑在 **Linux 宿主机 + systemd** 上，直接按这份文档走就行，不用再手动复制 service、手写 `web_config.json`、也不用自己改源码里的 Cookie 配置。

---

## 适用场景

适合这些情况：

- 你要部署在 Linux 宿主机上
- 你希望用 `systemd` 管理 cleaner / web / retention timer
- 你想快速完成安装，不想手动一项一项配

不适合这些情况：

- 你要直接跑 Docker / Docker Compose
- 你要完全手工定制部署细节，不想走交互式脚本

如果你要跑 Docker，请回 `README.md` 看 Docker 部署章节。

---

## 这个脚本会做什么

执行 `manager.sh install` 后，它会：

- 把当前仓库复制到 `/opt/CLIProxyAPI-cleaner`
- 交互式询问：
  - `base_url`
  - `management_key`
  - 控制台暴露模式
  - 监听端口
  - `allowed_hosts`
  - 控制台登录密码
- 自动生成 `web_config.json`
- 通过 systemd override 写入 `CLIPROXY_COOKIE_SECURE=true/false`
- 安装并启动：
  - `CLIProxyAPI-cleaner.service`
  - `CLIProxyAPI-cleaner-web.service`
  - `CLIProxyAPI-cleaner-retention.timer`

它**不会**去 `sed` 改 `app.py` 源码，所以后续升级更稳，不容易和代码变更打架。

---

## 前置要求

在执行前，建议确认：

- 系统是 Linux
- 已安装 `python3`
- 已安装 `systemd`
- 当前账号有 `root` / `sudo` 权限
- 服务器能访问你的上游管理端

如果你准备走 **反代 / HTTPS** 模式，建议也提前准备好：

- 域名
- Nginx / Caddy / 其他反代
- 证书

---

## 快速安装

### 1）获取代码

```bash
git clone https://github.com/KJ20051223/CLIProxyAPI-cleaner.git
cd CLIProxyAPI-cleaner
```

### 2）执行安装脚本

```bash
chmod +x manager.sh
sudo ./manager.sh install
```

---

## 安装过程中会问什么

### 1. `Management base_url`

填你的管理端地址，通常类似：

```text
https://your-domain.com/management.html
```

这是 cleaner 真正要访问的管理页面地址，不是控制台地址。

### 2. `Management key`

填管理 API 所需的密钥。

### 3. `Dashboard exposure mode`

这里有两种模式：

#### 模式 1：Reverse proxy / HTTPS

```text
1) Reverse proxy / HTTPS (listen on 127.0.0.1, secure cookie)
```

适合：

- 正式环境
- 你要挂到 Nginx / HTTPS 下
- 你不想让控制台直接裸露在公网端口上

脚本会默认：

- `listen_host = 127.0.0.1`
- `CLIPROXY_COOKIE_SECURE = true`

这是**推荐模式**。

#### 模式 2：LAN / direct HTTP

```text
2) LAN / direct HTTP (listen on 0.0.0.0, insecure cookie)
```

适合：

- 局域网内临时使用
- 你明确知道自己在走 HTTP 直连
- 没有 HTTPS 反代

脚本会默认：

- `listen_host = 0.0.0.0`
- `CLIPROXY_COOKIE_SECURE = false`

这个模式方便，但安全性比模式 1 弱，**不要默认当成公网正式方案**。

### 4. `Dashboard listen port`

默认是：

```text
28717
```

如果端口冲突，可以改成别的。

### 5. `Dashboard allowed_hosts`

填允许访问控制台的 Host，多个值用逗号分隔。

常见示例：

#### 反代 / HTTPS 模式

```text
127.0.0.1,localhost
```

#### 局域网 / HTTP 直连模式

```text
*,127.0.0.1,localhost
```

如果你知道自己会用固定域名访问，也可以收紧成：

```text
your-domain.com,127.0.0.1,localhost
```

### 6. `Dashboard password`

脚本会让你输入两次控制台密码，并自动生成：

- `password_salt`
- `password_hash`

不用自己手动算 PBKDF2。

---

## 安装完成后会落到哪里

默认安装路径：

```text
/opt/CLIProxyAPI-cleaner
```

关键文件：

- 程序目录：`/opt/CLIProxyAPI-cleaner`
- 配置文件：`/opt/CLIProxyAPI-cleaner/web_config.json`
- Web override：`/etc/systemd/system/CLIProxyAPI-cleaner-web.service.d/override.conf`
- cleaner 日志：`/root/CLIProxyAPI-cleaner.log`
- web 日志：`/opt/CLIProxyAPI-cleaner/web.log`
- 状态文件：`/root/CLIProxyAPI-cleaner-state.json`

---

## 安装后检查

### 查看服务状态

```bash
systemctl status CLIProxyAPI-cleaner-web.service --no-pager
systemctl status CLIProxyAPI-cleaner.service --no-pager
systemctl status CLIProxyAPI-cleaner-retention.timer --no-pager
```

### 看日志

```bash
tail -f /opt/CLIProxyAPI-cleaner/web.log
tail -f /root/CLIProxyAPI-cleaner.log
```

---

## 如何访问控制台

### 如果你选的是模式 1（反代 / HTTPS）

推荐把控制台挂到：

```text
https://your-domain.com/CLIProxyAPI-cleaner/
```

Nginx 示例：

```nginx
location ^~ /CLIProxyAPI-cleaner/ {
    proxy_pass http://127.0.0.1:28717;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_http_version 1.1;

    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy same-origin always;
    add_header Content-Security-Policy "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'" always;
}
```

改完后：

```bash
nginx -t && systemctl reload nginx
```

### 如果你选的是模式 2（局域网 / HTTP 直连）

直接访问：

```text
http://你的服务器IP:端口/CLIProxyAPI-cleaner/
```

比如：

```text
http://192.168.1.20:28717/CLIProxyAPI-cleaner/
```

---

## 后续升级

如果你后面从 GitHub 拉了新版代码，推荐这样升级：

```bash
cd /path/to/your/repo-clone
git pull origin main
sudo ./manager.sh install
```

安装脚本检测到 `/opt/CLIProxyAPI-cleaner/web_config.json` 已存在时，会问你：

```text
Reuse existing web_config.json and cookie override [Y]
```

如果你只是升级代码，想保留现有配置，直接选：

```text
Y
```

这样会：

- 覆盖程序文件到 `/opt/CLIProxyAPI-cleaner`
- 保留现有 `web_config.json`
- 保留现有 cookie override
- 重启相关 systemd 服务

---

## 重新改模式 / 重新生成配置

如果你要从：

- 局域网 HTTP 改成 HTTPS 反代
- 或者想重新设置 `base_url` / `management_key` / 登录密码

可以再次运行：

```bash
sudo ./manager.sh install
```

当脚本问你是否复用旧配置时：

```text
Reuse existing web_config.json and cookie override [Y]
```

如果你想重新生成，就选：

```text
N
```

然后按提示重新填写一遍。

---

## 卸载

```bash
sudo ./manager.sh uninstall
```

它会：

- 停止并禁用相关 systemd 服务
- 删除 systemd unit 和 override
- 删除 `/opt/CLIProxyAPI-cleaner`
- 删除日志和状态文件

执行前会二次确认。

---

## 常见问题

### 1）我只想升级，不想重新填配置

重新执行：

```bash
sudo ./manager.sh install
```

然后在复用配置提示里选 `Y`。

### 2）登录不了控制台

优先检查：

- 你当前访问方式和模式是否匹配
  - HTTPS 反代 → `COOKIE_SECURE=true`
  - HTTP 直连 → `COOKIE_SECURE=false`
- `allowed_hosts` 是否写对
- `CLIProxyAPI-cleaner-web.service` 是否正常启动

### 3）页面里保存了参数，但 cleaner 没变化

正常情况下，页面保存后配置会写进 `web_config.json`，`run_cleaner.py` 会按新配置启动 cleaner。

如果你刚改完又没生效，可以手动重启：

```bash
systemctl restart CLIProxyAPI-cleaner.service
```

### 4）要不要自己手动改源码里的 Cookie 配置

不用。

`manager.sh` 已经通过 systemd override 写 `CLIPROXY_COOKIE_SECURE`，不要再去手改 `app.py`。

---

## 一句话总结

- **宿主机 + systemd**：优先用 `manager.sh`
- **容器部署**：回 `README.md` 用 Docker / Docker Compose
