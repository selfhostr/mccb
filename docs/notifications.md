# Notifications

By the end of this page you will know how to be told when uploads, analyses and migrations finish, without watching the screen.

Channels: [ntfy](https://docs.ntfy.sh/), a generic webhook, and e-mail (SMTP). Configure them with `PUT /api/intake/settings` (`notifications` section, see [API](api.md)). Nothing is sent until `notifications.enabled` is true and at least one channel is enabled.

```json
{"notifications": {"enabled": true,
  "ntfy": {"enabled": true, "url": "https://ntfy.example.org", "topic": "mccb-7f3k9", "priority": 3, "token": "tk_..."}}}
```

## Channels

- **ntfy**: the message is published as JSON to the server root URL (`url`), with `topic`, `title`, `message` and `priority`. `token` is sent as a Bearer token and is optional.
- **webhook**: `POST` of `{"event","title","message","priority","ts","data"}` to `url`. `token`, if set, is sent as `Authorization: Bearer`.
- **email**: `host`, `port`, `security` (`ssl`, `starttls`, `none`), optional `username` and `password`, `from`, `to`.

`POST /api/intake/notifications/test` sends a test message to one channel (`{"channel":"ntfy"}`) or to every enabled channel, and returns the result of each.

## Events

Each event can be switched on or off in `notifications.events`:

| event | default |
|-------|---------|
| `upload_finished` | off |
| `analysis_valid` | off |
| `analysis_invalid` | on |
| `job_done`, `job_done_with_errors`, `job_failed` | on |
| `job_stopped` | off |
| `phase_mail`, `phase_calendar`, `phase_contacts` | off |
| `batch_finished` | on |
| `disk_low` (below `disk_low_mb`, 5120 by default; at most once every 6 hours) | on |
| `repeated_crash` (a job crashed at least twice) | on |

When several jobs finish together, one message is sent instead of one per job: events that arrive within `group_window` seconds (30 by default) of each other are grouped, and when the queue becomes empty after two or more jobs, a single "Batch finished" message summarises them. A batch started with a name (`POST /api/runs` with `name`) is announced by name when its last job ends, with the jobs that failed or ended with errors listed; those jobs then do not appear in the generic summary, and the individual messages of the ones that went well are dropped. A job that is paused to wait for the allowed hours of its server sends nothing.

## Privacy

`mask_addresses` replaces e-mail addresses in messages with a masked form (`j***@e***.com`), file names included.

A public ntfy.sh topic can be read by anyone who knows its name. Use a long random topic, or your own ntfy server with an access token, and consider masking addresses.

## Delivery

Notifications are sent from a background thread with a 5 s connect and 10 s total timeout and at most three attempts. A failure is logged and the message is dropped; it never slows or stops a migration. The destinations follow the address policy described in [Security](security.md).
