# Tune builders, connections and mailboxes

Everything on this page is automatic. Set a value only to impose a limit: a target server that must be spared, a machine shared with other work.

## Mailboxes at once

`WORKER_CONCURRENCY` unset or `0` (the default) means auto. A `.env` copied from an earlier `.env.example` pins it to `2`: remove that line to get the auto mode.

In auto mode the worker looks at the machine every 10 s:

- **Cores:** 2 per mailbox, out of the usable cores minus one (cgroup quota and affinity respected).
- **Memory:** 512 MB per mailbox. File cache counts as free, including inside a container with a memory limit.
- **Queue:** never more than the jobs waiting. At most 32.
- **Idle cores:** mailboxes often wait for their target, not for the CPU. Every 30 s, if at least 2 cores stayed idle, jobs are waiting, memory and disk are fine and no target reported an error in the last 60 s, one more mailbox is allowed. With no idle core, one less. Nothing running is stopped.
- **Limits:** with less than 1 GiB free, or a saturated disk, no new mailbox starts while one is running. The disk counts as saturated when tasks waited for I/O more than 50 % of the last 10 s (`/proc/pressure/io`). Without that file: when the device holding the PSTs is busy more than 95 % of the time.

Each change is a line in the worker log (`concurrency (auto): ...`).

## Builders and connections of one mailbox

Each server has a **tuning** mode (server form: Auto / Manual).

- **Manual**: `workers` IMAP APPEND connections and `builders` builder processes (empty = CPUs − 1, max 32).
- **Auto** (default): the job starts with its share of the cores as builders (at most 32) and 4 IMAP connections. During the mail phase a sample is taken every 5 s (`AUTOTUNE_INTERVAL`) and the controller adjusts both counts, one change per sample at most:
  - More connections while built messages wait for an APPEND slot and it pays off: +2, or +25 % above 8 connections. Throughput is compared over 2 samples, since mail sizes vary. An increase that brought less than 5 % is undone. That level is tried again after 15 minutes.
  - More builders while they are the bottleneck, a core is idle and memory is free. Back to its share of the cores when no core is idle: a mailbox started alone gives back what it took as others arrive.

Each change is a job log line (`Auto-tune: ...`); the job page shows the current values.

## What protects the target server

- **Limits:** at most 32 connections per mailbox (`AUTOTUNE_MAX_CONNECTIONS`) and 64 per server across all its mailboxes (`AUTOTUNE_MAX_SERVER_CONNECTIONS`). A mailbox above its share of the server goes down to it.
- **Back-off:** on a transient error (`[LIMIT]`, `[UNAVAILABLE]`, BYE, too many connections, throttling, refused, timeout), connections are halved at once. Every connection of the job then waits before its next message (from 1 s, doubling up to 30 s while errors continue, with jitter), and no mailbox on that server grows for 60 s.
- **Latency:** APPEND latency is compared per message-size bucket with a smoothed baseline. A back-off needs the excess for 2 consecutive samples and a throughput drop.
- **Ceiling:** the count that failed is not reached again, unless it had run clean before: one network error is not a limit, a second one at that count is. The ceiling rises by 1 after 30 minutes without any error.
- **Fallback:** after 3 back-offs within 10 samples, at most 2 builders and 2 connections. It ends after 15 minutes without any error, and the job can then grow back.
- **DAV:** on HTTP 429 or 503, a `Retry-After` given by the server is honoured, up to 5 minutes.

## Memory

- Built messages waiting for an APPEND are bounded by `BUILD_MAX_INFLIGHT_MB`. Unset, in auto mode: a quarter of the free memory split between the running mailboxes, between 256 MiB and 4 GiB.
- With less than `MEM_GUARD_MB` (512) free, builds run one at a time until memory comes back. The job log says so.
- A builder killed by SIGKILL (out of memory) is not a libpff crash: its item is retried once with one builder fewer, then marked "builder process killed (SIGKILL, probably out of memory)".
- A single message is not split: see [Known limitations](limits.md).

## What does not change

- Resizing is graceful: a connection or builder leaves only after its current item is finished and recorded; queued items stay queued.
- An item row is committed as `inflight` before its APPEND, always. Results are written in batches (at most 50 ms or 256 items later). If the process dies first, those items are still `inflight` and searched on the server on resume, so nothing is sent twice.
- Per-item verification and `X-PST-Import-Id` idempotency are unchanged.
- Upgrade: servers of earlier versions become Auto when `workers` was a default (2 in the 1.0.0 UI, 4 in the schema); any other value was chosen by hand and stays Manual with that value.

## Measure

`tests/perf/bench_pipeline.py` runs the real web, worker and engine against GreenMail and writes JSON: upload MB/s, analysis seconds, items/s and MB/s per phase, resume time, peak RSS. `--db-rows` adds the database costs on a table of that size.

```bash
docker compose -f docker-compose.test.yml run --rm --build tests \
  python tests/perf/bench_pipeline.py --fixtures enron.pst,aspose-outlook.pst --mailboxes 1,4 \
  --db-rows 1000000 --output /app/bench.json
docker compose -f docker-compose.test.yml down -v
```

The fixtures are small and GreenMail is not a production server. Compare two versions on one machine rather than reading the numbers as what a real migration will reach.
