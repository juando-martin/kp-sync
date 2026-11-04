# Open review findings

Open code-review findings, most important first. Remove an entry when it is
fixed (the commit records how) or rejected (record why in the owning design
chapter if it is a design decision). Durable lessons belong in
[REVIEW.md](REVIEW.md); acceptance status belongs in [PLAN.md](PLAN.md).

Source: review on `beta` on 2026-11-03. Findings were first made on
`short-status-cleanup-service` (`db06c97`) and re-checked on `main` at
`v0.2.0` (`be12d05`). Line numbers refer to `v0.2.0`. Nothing was changed,
installed or contacted during the review.

**Confirmed** means reproduced or traced through the code end to end.
**Plausible** means traced in code but needing a timing window or an
uncommon configuration.

## Agreed order of work

Round 1 (F1–F3) shipped as `v0.2.1`. Remaining rounds, each on its own
branch with tests, committed locally and pushed only when work moves to
`alpha`:

F4–F11 are fixed on `main` and released as `v0.3.0` (not pushed).
Upgrading Black in `alpha`'s tools environment (F7) is a package install
and needs its own approval there. Acceptance beyond unit tests needs the
`kp-test` profile; installing kp-sync or units on `beta` needs owner
approval, otherwise use `--no-daemon` runs from the checkout.

F5 and F11 were done in the unattended run of 2026-11-04, each on its own
branch (merged into `main` for `v0.3.0`), stacked in this order: `f5-probe-content-hash`,
`f11-batch-bases` (part 1, batched base transfers) and
`f11-rcd-transport` (part 2, the optional `rcd` transport, default off).
The decisions are in `design/history-and-sync.md` ("Sync algorithm"
step 2 and "Save") and `design/endpoints-and-access.md` ("Optional rcd
transport"); No findings remain open.

## Scope

Read closely (at `db06c97`, re-checked at `v0.2.0`): file and drive
endpoints, the engine's observation, write and commit path, baseline
publication, `cleanup.py`, `services.py` daemon start/stop, short status
hashes, the kp-access bridge and operations, session `authorized_keys`
editing and `sftp-serve` confinement, credential caching and the rclone
endpoint.

Also read: every source change from `db06c97` to `v0.2.0`
(`drive_sync.py`, `rclone_credentials.py`, the daemon credential watch and
draining, rclone endpoint and engine hooks, pendrive history retention, and
`service install|enable|disable|uninstall daemon`).

Not yet reviewed in detail: the unchanged parts of `daemon/server.py`,
`merge.py`, `sessions/session.py`, `config.py` and `integration.py`, and
the UI modules.

F1–F3 (test isolation: real daemon socket, checkout-location and umask
dependence, KeePassXC 2.7 `import`) were fixed on `fix-test-isolation`;
the full suite (443 tests) then passed on `beta`.

## Reviewed without findings

- File and drive promotion: `RENAME_EXCHANGE`, displace-then-install on
  exFAT, `RENAME_NOREPLACE` preflight, displaced-file recovery and drive
  identity rechecks around writes.
- Engine writes: backup and recheck before each write, in-flight candidate
  records before partial promotion, commit only after final confirmation.
- `cleanup.py`: whole-graph protection, recovery blockers, per-deletion
  rechecks, explicit scope and paused-writer confirmation.
- `services.py` daemon start/stop: config-path refusal, clean-shutdown
  verification and protocol readiness.
- kp-access: virtual path validation, private upload staging, KDBX checks
  before installing the database and backups before replacement.
- Sessions: `authorized_keys` editing touches only tagged lines, under a
  lock, with an identity check; `sftp-serve` uses bwrap confinement by
  default and needs an explicit opt-out.
- Credentials: keyring access goes through the `keyctl` library, never
  argv; keys are secured or revoked on failure.
- Drive role selection (fixed in `v0.2.0`): `drive-sync` requires
  `--mode main|secondary`, and `~/.dynamic_config` is no longer used.
- `drive_sync.py`: identity verified against the mounted filesystem before
  the pending-up marker is written under `keydisk.lock`; the marker is
  kept when submission fails; `--config`/`--profile` are mutually
  exclusive, so the right daemon is addressed.
- `rclone_credentials.py` itself: the external file is only read
  (`O_NOFOLLOW`, size cap, regular-file check); the private copy is written
  atomically with private modes, and symlinks are refused.
- Daemon activation controls: `enable` requires installed units;
  `disable`/`uninstall` stop triggers before the daemon and verify a clean
  shutdown; `uninstall` removes only unmodified generated files after a
  preflight and rechecks each file before unlinking it.
