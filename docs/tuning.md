# Tune builders and IMAP connections

Each server has a **tuning** mode (server form: Auto / Manual).

- **Manual**: `workers` IMAP APPEND connections and `builders` builder processes (empty = CPUs − 1, max 8).
- **Auto** (default): at job start the machine is probed (usable cores, free memory, ~1.5 s build benchmark);
  the builders are the free cores split between the running jobs, the job starts with 4 IMAP connections.
  During the mail phase a sample is taken every 5 s and the controller adjusts both counts: more connections
  while built messages wait for an APPEND slot and it pays off, more builders while they are the bottleneck and
  CPU is idle, connections halved on a transient server error ([LIMIT], [UNAVAILABLE], BYE, refused, timeout),
  APPEND latency compared per message-size bucket with a smoothed baseline (a back-off needs the excess for
  2 consecutive windows and a throughput drop), and a latched fallback after repeated errors (at most 2
  builders / 2 connections, never more than the current values; errors still back off while in fallback).
  A builder killed by SIGKILL (out of memory) is not a libpff crash: its item is retried once with one builder
  fewer, then marked "builder process killed (SIGKILL, probably out of memory)". Each change is a job log line
  (`Auto-tune: ...`); the job page shows the current values.
- Resizing is graceful: a connection or builder leaves only after its current item is finished and recorded;
  queued items stay queued. Per-item verification and `X-PST-Import-Id` idempotency are unchanged.
- Upgrade: servers of earlier versions become Auto when `workers` was a default (2 in the 1.0.0 UI, 4 in the
  schema); any other value was chosen by hand and stays Manual with that value.
