# Upgrade mccb

By the end of this page mccb will run a newer image with your data intact.

```bash
docker compose pull && docker compose up -d
```

If you pinned a version, change the tag in `docker-compose.yml` first. Read the [changelog](https://github.com/yetysw/mccb/blob/main/CHANGELOG.md) before a minor or major version.

Back up `./data` first: see [Back up and restore](backup.md).

## Check the version

The version is shown in the sidebar footer and on the login screen, returned by `GET /api/version` (public) and `GET /api/health`, and logged when `web` and `worker` start.

It comes from `app/VERSION`. Docker builds can override it with `--build-arg VERSION=<tag>` (CI passes the tag) and `--build-arg GIT_SHA=<sha>`. Without `VERSION` the app shows `<file>+<sha>`, for example `2.0.0+<sha>`.

## Notes for older installs

Servers created by earlier versions become Auto tuning when `workers` was a default (2 in the 1.0.0 UI, 4 in the schema). Any other value was chosen by hand and stays Manual. See [Tune builders and IMAP connections](tuning.md).

## Stop and start without losing a job

On `SIGTERM` the worker gives subprocesses `CHILD_TERM_GRACE` seconds (20 by default) to stop, then jobs go back to the queue and resume later. The compose file allows the worker 30 seconds (`stop_grace_period`).
