# Install MCCB

By the end of this page MCCB will be running in Docker and you will be signed in to the dashboard.

## Requirements

- Docker with Compose.
- A published image for `linux/amd64` (`ghcr.io/yetysw/mccb`). No `.env` file and no build are needed.
- Disk space for the PST files you upload, in `./data/pst/`. Uploads check free space first and answer 507 when there is not enough.

## Start it

```bash
mkdir mccb && cd mccb
curl -fsSLO https://raw.githubusercontent.com/yetysw/mccb/main/docker-compose.yml
docker compose up -d
docker compose logs web | grep -A2 "Admin password"
```

The compose file runs two services from the same image:

| service  | role |
|----------|------|
| `web`    | FastAPI API and static UI, port 22362 |
| `worker` | job queue (one mailbox is one job) and validation of uploaded PSTs |

Open `http://<host>:22362` and sign in with the printed password. It is shown once, in the `web` log. Change it under **Settings**, or see [Change or reset the admin password](admin-password.md).

`GET /api/health` is public and also reports whether the worker is alive (heartbeat less than 60 s old).

> **Warning.** Port 22362 speaks plain HTTP. Put MCCB behind a [reverse proxy](reverse-proxy.md) and do not expose the port directly.

## Pin a version

The image is tagged `X.Y.Z`, `X.Y` and `latest`. To pin a version, replace `latest` with the tag in `docker-compose.yml`, for example `1.1.2`.

## Build from source

```bash
git clone https://github.com/yetysw/mccb.git
cd mccb
docker compose up -d --build
docker compose logs web      # read the generated admin password
curl http://localhost:22362/api/health
```

## Use your own settings

Copy `.env.example` to `.env` and edit it. `ADMIN_PASSWORD` and `SECRET_KEY` are both optional. To generate a `SECRET_KEY`:

```bash
openssl rand -base64 32
# or: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Every variable is listed in [Configuration](configuration.md).

## Where data lives

All data is in `./data`, mounted on `/data`:

- `app.db`: SQLite database (WAL);
- `pst/`: PST files;
- `reports/job-N.json`: reports;
- `logs/job-N.log`: logs;
- `admin.json`: hashed admin password, generated on first start if `ADMIN_PASSWORD` is not set;
- `secret.key`: key generated if `SECRET_KEY` is empty.

Back it up: see [Back up and restore](backup.md).
