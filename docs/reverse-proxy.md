# Serving the web UI over HTTPS

How `https://k6v3.mckero.dn42/` is set up on this host. The emulator UI itself
speaks plain HTTP on loopback; nginx terminates TLS and proxies to it.

## Why a proxy at all

`tools/webui.py` has no TLS and no authentication. Running it on loopback and
letting nginx face the network means the existing certificate and the existing
443 listener are reused, and the UI is not directly reachable at all.

## The vhost

Lives in `/etc/nginx/sites-available/k6v3`, symlinked into `sites-enabled/`. A copy
is kept in this repo at [`deploy/nginx-k6v3.conf`](../deploy/nginx-k6v3.conf), since
nothing else here version-controls `/etc`.

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

Start the server bound to loopback, since nginx is the only thing that needs to
reach it:

    python3 tools/webui.py --frame-addr 0x200013DC --status-addr 0x2000175C \
        --host 127.0.0.1

## The settings that are not optional

**`proxy_buffering off`.** `/stream` is an endless
`multipart/x-mixed-replace` response. With buffering on, nginx holds frames back
and the picture arrives in bursts or appears frozen. This is the single setting
most likely to be dropped when someone rewrites the vhost.

**`proxy_read_timeout` well above the idle frame interval.** The stream sends a
keepalive frame every `IDLE_FRAME_INTERVAL_S` (2 s), so the default 60 s would be
survivable -- but a paused guest produces nothing at all, and the default would
then drop the connection.

**`X-Forwarded-For`.** The log pane attributes each line to a client IP. Behind a
proxy `REMOTE_ADDR` is always 127.0.0.1, so without this header every entry reads
as if it came from the server itself. `webui.py` trusts only the first hop.

**`tcp_nodelay on`.** Keypress responses are small and frequent; Nagle would add
delay to exactly the requests where latency is the point.

## No new address, no new certificate

Both are deliberate:

- 443 is shared with the other vhosts on these addresses and separated by SNI, so
  no additional IP is consumed.
- The existing `*.mckero.dn42` wildcard already covers this name, so nothing had
  to be issued. Check it with:

      openssl x509 -in /etc/letsencrypt/live/mckero-wildcard/fullchain.pem \
          -noout -text | grep -A1 'Subject Alternative Name'

Only DN42 addresses are bound. The public addresses on this host also listen on
443, and they are untouched -- the exposure is decided by the `listen` address, so
the UI is not reachable from the internet.

## DNS

Records to point at it:

    k6v3.mckero.dn42.  A     172.21.91.140
    k6v3.mckero.dn42.  AAAA  fd3c:3f9b:6424:2::5

## Pitfalls hit while setting this up

**`http2 on;` needs nginx 1.25.1+.** This host runs 1.22.1, where that directive
does not exist. Worse, `nginx -t` was run *before* the symlink was created, so it
passed, and the subsequent `systemctl reload` failed and left nginx **stopped** --
taking the other sites down until the line was removed. Create the symlink first,
then `nginx -t`, then reload. For HTTP/2 on 1.22 the syntax is
`listen ... ssl http2;`.

**A new `listen` address needs a reload to take effect.** After the failed reload
above, `systemctl start` brought nginx back but it had not bound
`[fd3c:3f9b:6424:2::5]:443`; v6 requests failed with no error in the log. A second
`systemctl reload nginx` created the socket. If a newly added address refuses
connections, check `ss -ltnp | grep 443` before looking anywhere else.

## Verifying

    # both families, and check the certificate rather than skipping it with -k
    curl -s -o /dev/null -w '%{http_code}\n' \
        --resolve 'k6v3.mckero.dn42:443:172.21.91.140' \
        https://k6v3.mckero.dn42/
    curl -s -g -o /dev/null -w '%{http_code}\n' \
        --resolve 'k6v3.mckero.dn42:443:[fd3c:3f9b:6424:2::5]' \
        https://k6v3.mckero.dn42/

    # the stream must deliver frames continuously, not in one burst at the end
    curl -sk --resolve 'k6v3.mckero.dn42:443:172.21.91.140' \
        https://k6v3.mckero.dn42/stream | head -c 20000 | grep -c PNG

    # log attribution: entries should carry the real client address, not 127.0.0.1
    curl -sk --resolve 'k6v3.mckero.dn42:443:172.21.91.140' \
        https://k6v3.mckero.dn42/api/logs

Measured after setup: HTTP 200 on both families with the certificate validating,
first stream frame in 0.01 s, 7 frames in 12 s on an idle screen, and log entries
attributed to `172.21.91.140` and `fd3c:3f9b:6424:2::5` while firmware serial and
QEMU lines correctly show no client.
