# 用 HTTPS 提供网页界面

`https://k6v3.mckero.dn42/` 在这台主机上是怎么配起来的。模拟器界面本身只在 loopback 上
讲普通 HTTP；由 nginx 终结 TLS 并反代到它。

*English: [reverse-proxy.md](reverse-proxy.md) · 两份内容对应，改动请同步。*

## 为什么要用反向代理

`tools/webui.py` 既没有 TLS 也没有任何认证。让它跑在 loopback 上、由 nginx 面向网络，
意味着可以复用已有的证书和已有的 443 监听，而且**这个界面根本不能被直接访问到**。

## vhost 配置

放在 `/etc/nginx/sites-available/k6v3`，软链接进 `sites-enabled/`。仓库里在
[`deploy/nginx-k6v3.conf`](../deploy/nginx-k6v3.conf) 存了一份副本，
因为这里没有别的东西给 `/etc` 做版本控制。

    server {
        listen 172.21.91.140:80;
        listen [fd3c:3f9b:6424:2::5]:80;
        server_name k6v3.mckero.dn42;
        return 301 https://$host$request_uri;
    }

    server {
        listen 172.21.91.140:443 ssl;
        listen [fd3c:3f9b:6424:2::5]:443 ssl;
        server_name k6v3.mckero.dn42;

        ssl_certificate     /etc/letsencrypt/live/mckero-wildcard/fullchain.pem;
        ssl_certificate_key /etc/letsencrypt/live/mckero-wildcard/privkey.pem;

        location / {
            proxy_pass http://127.0.0.1:8080;
            proxy_http_version 1.1;

            proxy_set_header Host              $host;
            proxy_set_header X-Real-IP         $remote_addr;
            proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;

            proxy_buffering off;
            proxy_request_buffering off;
            proxy_read_timeout 3600s;
            proxy_send_timeout 3600s;
            tcp_nodelay on;
        }
    }

启动服务时绑定到 loopback，因为只有 nginx 需要访问它：

    python3 tools/webui.py --frame-addr 0x200013DC --status-addr 0x2000175C \
        --host 127.0.0.1

## 那些不能省的配置项

**`proxy_buffering off`。** `/stream` 是一个无限长的
`multipart/x-mixed-replace` 响应。开着缓冲的话，nginx 会把帧攒起来，
于是画面一阵一阵地到、或者看起来卡死了。**这是别人重写 vhost 时最可能漏掉的一项。**

**`proxy_read_timeout` 要远大于空闲帧间隔。** 这个流每 `IDLE_FRAME_INTERVAL_S`（2 秒）
发一个保活帧，所以默认的 60 秒本来还能活 —— 但**一个被暂停的 guest 什么都不产生**，
那时默认值就会掉连接。

**`X-Forwarded-For`。** 日志面板会把每一行归属到一个客户端 IP。在代理后面
`REMOTE_ADDR` 恒为 127.0.0.1，所以**没有这个头的话每一条都会显示成来自服务器自己**。
`webui.py` 只信任第一跳。

**`tcp_nodelay on`。** 按键响应又小又频繁；Nagle 算法会恰好给那些**以延迟为关键**的
请求增加延迟。

## 不需要新地址，也不需要新证书

两者都是刻意的：

- 443 与这些地址上的其他 vhost 共用，靠 SNI 区分，所以不占用额外的 IP。
- 已有的 `*.mckero.dn42` 通配证书本来就覆盖这个名字，所以不需要签发任何东西。这样检查：

      openssl x509 -in /etc/letsencrypt/live/mckero-wildcard/fullchain.pem \
          -noout -text | grep -A1 'Subject Alternative Name'

**只绑定了 DN42 地址。** 这台主机上的公网地址同样在 443 上监听，而它们**完全没被碰过** ——
**暴露范围是由 `listen` 地址决定的**，所以这个界面从互联网上访问不到。

## DNS

指向它的记录：

    k6v3.mckero.dn42.  A     172.21.91.140
    k6v3.mckero.dn42.  AAAA  fd3c:3f9b:6424:2::5

## 配置过程中踩到的坑

**`http2 on;` 需要 nginx 1.25.1+。** 这台主机跑的是 1.22.1，那条指令不存在。
更糟的是 `nginx -t` 是在**创建软链接之前**跑的，所以它通过了，
而随后的 `systemctl reload` 失败并让 nginx **停止运行** ——
在那一行被删掉之前，其他站点全都断了。
**先建软链接，再 `nginx -t`，然后 reload。** 在 1.22 上启用 HTTP/2 的语法是
`listen ... ssl http2;`。

**新加的 `listen` 地址需要 reload 才会生效。** 在上面那次失败的 reload 之后，
`systemctl start` 把 nginx 拉回来了，但它**没有绑定** `[fd3c:3f9b:6424:2::5]:443`；
v6 请求失败，而**日志里没有任何错误**。第二次 `systemctl reload nginx` 才创建了那个 socket。
如果一个新加的地址拒绝连接，**先看 `ss -ltnp | grep 443`**，再去别处找。

## 验证

    # 两个协议族都测，而且要校验证书，不要用 -k 跳过
    curl -s -o /dev/null -w '%{http_code}\n' \
        --resolve 'k6v3.mckero.dn42:443:172.21.91.140' \
        https://k6v3.mckero.dn42/
    curl -s -g -o /dev/null -w '%{http_code}\n' \
        --resolve 'k6v3.mckero.dn42:443:[fd3c:3f9b:6424:2::5]' \
        https://k6v3.mckero.dn42/

    # 这个流必须持续投递帧，而不是最后一次性全来
    curl -sk --resolve 'k6v3.mckero.dn42:443:172.21.91.140' \
        https://k6v3.mckero.dn42/stream | head -c 20000 | grep -c PNG

    # 日志归属：条目里应该带真实客户端地址，而不是 127.0.0.1
    curl -sk --resolve 'k6v3.mckero.dn42:443:172.21.91.140' \
        https://k6v3.mckero.dn42/api/logs

配置完成后的实测：两个协议族都是 HTTP 200 且证书校验通过，第一帧在 0.01 秒到，
空闲画面下 12 秒 7 帧，日志条目正确归属到 `172.21.91.140` 和
`fd3c:3f9b:6424:2::5`，而固件串口和 QEMU 的日志行正确显示没有客户端。
