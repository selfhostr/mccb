# Put MCCB behind a reverse proxy

By the end of this page MCCB will be served over HTTPS with uploads and live updates working.

The `web` service listens on plain HTTP on 22362 and trusts `X-Forwarded-*` headers
(uvicorn `proxy_headers`, all proxies accepted): the client IP for login rate limiting and the
HTTPS scheme for the cookie. **Do not expose it directly to the Internet in addition to the proxy**: filter
port 22362 (for example `127.0.0.1:22362:22362` in the compose file) so that only the proxy can reach it.

Essential points:

- **No request buffering**: the 32 MiB chunks are streamed to the application;
  otherwise the proxy first stores them on disk (slow, and consumes space).
- **No response buffering** for `/api/events` (SSE), otherwise updates do not arrive.
- **Max body size** greater than or equal to `UPLOAD_CHUNK_SIZE` (32 MiB by default: 64 MiB leaves
  headroom).
- **Generous timeouts** (e.g. 3600 s): the SSE stream stays open, and the preview or a slow chunk can take a while.
- **`COOKIE_SECURE=1`** over HTTPS, and forward `X-Forwarded-For` / `X-Forwarded-Proto` / `Host`.

nginx example:

```nginx
server {
    listen 443 ssl http2;
    server_name migration.example.com;
    # ssl_certificate ... ; ssl_certificate_key ... ;

    client_max_body_size 64m;          # >= UPLOAD_CHUNK_SIZE
    proxy_request_buffering off;       # chunks streamed, not stored
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;

    location / {
        proxy_pass http://127.0.0.1:22362;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /api/events {             # SSE
        proxy_pass http://127.0.0.1:22362;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
    }
}
```

Caddy: `reverse_proxy 127.0.0.1:22362` works almost as is (automatic HTTPS, no body limit by
default, `X-Forwarded-*` headers added). For SSE, add `flush_interval -1` inside the
`reverse_proxy` block, and do not reduce the timeouts.
Traefik: no body limit by default (do not enable the `buffering` middleware with
a too-low `maxRequestBodyBytes` or a `memRequestBodyBytes` that buffers), increase if needed the
`respondingTimeouts` (`readTimeout`, `writeTimeout`, `idleTimeout`) of the entrypoint, and forward the
`X-Forwarded-*` headers (enabled by default).
