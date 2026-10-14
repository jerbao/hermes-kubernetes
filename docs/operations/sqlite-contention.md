# Diagnose SQLite contention and slow UI polls

`db-lock` measures a **SessionDB instance's connection lock**, not a global
SQLite writer lock. Each message identifies the database path, process ID,
instance ID, and `mode=ro` or `mode=rw`. Compare these fields before attributing
a slow reader to a writer waiting on the same lock. Separate processes can
still contend through SQLite's transaction locks.

Read-only handles intentionally serialize access to their own connection.
They do not use the writable handle's read pool, and their `_wal_active` field
is not populated by writer initialization. A slow `mode=ro` acquisition does
not prove that WAL is disabled or the writer's pool is exhausted.

For writable handles, `db-read fallback` reports why a read uses the protected
writer connection. Reports are limited to one per reason per instance every
five minutes:

- `wal_unconfirmed`: the handle could not confirm WAL during initialization.
- `read_budget_unavailable`: per-file/process connection budget or descriptor
  headroom prevented another connection; this does not necessarily mean that
  all eight per-file slots are busy.
- `open_failed:<exception>` and `open_backoff`: opening a reader failed, then
  subsequent opens were temporarily suppressed.
- `closed`: the connection pool is closing.

The fallback preserves connection serialization and write safety. Do not
force WAL, remove locks, or raise pool limits based solely on lock durations.
Query `PRAGMA journal_mode` without assigning a value and inspect the actual
mount: a network-backed block device mounted as ext4 is not CephFS.

`session.context_breakdown`, `subagent.list`, and `pet.info.meta` run in the
existing RPC executor. Their response IDs and transport context are preserved;
a slow poll no longer holds the serial WebSocket dispatcher. This does not
make the underlying query faster or remove executor capacity limits.

Also inspect cgroup `memory.events` and `memory.stat`. Repeated `max` events,
`pgscan_direct`, and `workingset_refault_file` can expose memory reclaim even
when no OOM occurred. SQLite page cache and mapped files count toward the
container's memory limit. Set requests and limits in the instance manifests
based on workload and node capacity rather than treating every delay as a
SQLite locking problem.

Remote terminal upload timeouts and local validation of an SSH-only working
directory are separate problems. A producer blocked writing its archive to
SSH is not evidence of a database lock. Preserve the transfer and checkpoint
semantics when investigating those paths.
