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

1. **F9, then F8** (owner agreed to proceed). Record F9's best-effort
   pendrive publication in `design/history-and-sync.md` before coding.
   Acceptance beyond unit tests needs the `kp-test` profile; installing
   kp-sync or units on `beta` needs owner approval, otherwise use
   `--no-daemon` runs from the checkout.
2. **F4**, after an owner decision. Recommended: upload missing bases from
   the fresh listing; when a base exists nowhere, keep refusing to
   publish, name the version, and add an explicit repair command rather
   than dropping versions automatically (they may be merge ancestors).
3. **F7 and F5**, after owner decisions. Recommended: move both laptops to
   Black 26 and reformat in a separate commit (`alpha` has Black 25.12,
   which is clean today); widen the Sphinx pin only if the strict build
   passes under 9.2. For F5, take the probe shortcut only when the token
   includes a content hash; `command` endpoints always download.
4. **F6**: leave open until a Flatpak or custom-config KeePassXC is used.

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

## History publication

### F4. A missing remote base blocks cloud publication (plausible)

`BaselineManager.save` requires a reachable base for every retained version
(`src/kp_sync/baseline.py:430`) but uploads a base to a store only when its
version is absent from that store's recorded state (`:440-442`). Nothing
restores a base that is listed in a store's state but missing as a file.
Runtime base caches are pruned to retained versions and lost at logout.

Since `v0.1.3`, configured pendrives also keep retained bases, so
`ensure_base` can find a base missing from the cloud while such a pendrive
is attached. The cloud copy is still never re-uploaded, though: the failure
("cannot publish history: base … is unavailable") returns on every host
without a cached copy whenever no pendrive holding the base is attached.
The same applies to a base missing from a pendrive store.

One way to get there, with no cross-host lock: host A publishes, re-checks,
then deletes stale bases from the listing taken by that check
(`:451-457`). If host B checked before A's publish and writes its state
between A's re-check and A's deletes, A can delete a base that B's
just-published state references. The window is several seconds for the
cloud, because pruning deletes eight or more files one rclone call at a
time. Manual or Drive-side deletion has the same effect.

Fix: decide uploads per store from the fresh base listing that `check`
already fetches, not from recorded versions. That also fixes the cost
below. Consider whether a retained version whose base exists nowhere should
block all publication or be dropped from the graph with a diagnostic.

Related efficiency issue: after a reboot, the first publish calls
`ensure_base` for every retained version and downloads every uncached base,
even though only bases missing from a target need uploading.

### F5. Size and modification time are trusted as content identity (plausible)

`_observe` (`src/kp_sync/engine.py:1489-1513`) skips the download and
treats a copy as its recorded version when the probe token equals the
stored `remote_hashes["probe"]`. That value is stored unconditionally
(`:1405`). For rclone it hashes `{hashes, size, mtime}`. Google Drive
includes MD5, so it is safe. A hashless remote reduces it to size and
modification time, and a `command` endpoint's `probe` can return anything;
`design/architecture.md:80` explicitly allows "hash or size+mtime".

If such a copy is rewritten without its token changing (same size; same
mtime within the backend's precision), the engine treats it as the old
version. A later promotion then backs up the cached base instead of the
real file and overwrites the edit. This conflicts with the REVIEW lesson
that timestamps alone do not establish content identity. Fix: take the
shortcut only when the token includes a content hash, and document that a
command `probe` must change whenever content changes.

## Code new in v0.2.0

### F8. Private rclone copy is not protected across processes (plausible)

`RcloneCredentials.refresh()` and `finish()`
(`src/kp_sync/rclone_credentials.py:81-141`) require that no rclone process
of the profile is running, because rclone writes refreshed tokens back to
the file named by `--config`. That holds only inside one process: the
engine calls them under its sync lock (`engine.py`, `begin_run`/`end_run`),
and the daemon's `_watch_credentials` (`daemon/server.py:347-367`) drains
only when its own sync gate is idle. Other processes use the same private
copy without that lock:

- `kp-sync history` loads cloud history through it with no lock
  (`history_command.py:38-48`);
- `doctor` calls `refresh()` directly with no lock
  (`integration.py:1133`).

Failure scenarios:

- **Refresh during another process's run:** a refresh imports a new
  provider file while a parallel rclone still holds the old one. That
  rclone then writes its refreshed token from the old config over the
  import. `.source` already records the new digest, so the newer config is
  never imported again until the provider changes it once more. This
  matters when the update was a re-authorization; Google Drive refresh
  tokens don't rotate, so an old copy usually keeps working.
- **Drain during another process's run:** a drain deletes the copy while a
  parallel rclone is running. If that rclone later saves a token, it
  recreates the copy (udevmon proposal R3), so credentials survive after
  the drive is removed until the next drain.

Fix: serialize every rclone invocation and every refresh/finish/discard of
a profile's copy under one lock: a dedicated `rclone-credentials.lock`,
shared for rclone runs and exclusive for refresh and drain.

### F9. A pendrive history failure fails the whole sync (plausible)

Since `v0.1.3`, `BaselineManager.save` publishes to the primary cloud and
then every attached writable pendrive in one loop, re-raising any store
error (`baseline.py:406-462`). The engine commits runtime state before
calling it and does not catch the error, so `_finish_inflight_candidates`,
`_prune_cached_bases`, `_clear_pending` and `_finish_write_race` are
skipped and the run reports failure. A pulled drive surfaces as
`BaselineChanged` and the run restarts cleanly, but a persistent drive
error (exFAT full, read-only remount, I/O error) makes every sync fail
while that drive is attached, even though the cloud publish succeeded.
Fix: treat pendrive replicas as best-effort after the primary target
(record a `failed:` publication outcome and continue), keeping the
existing behavior when a drive is the only reachable store.

## Minor

### F6. KeePassXC reload setting read from one fixed path (confirmed)

`src/kp_sync/daemon/apps.py:261` and `src/kp_sync/integration.py:704`
read only `~/.config/keepassxc/keepassxc.ini`. A non-default
`XDG_CONFIG_HOME` or a Flatpak KeePassXC
(`~/.var/app/org.keepassxc.KeePassXC/config/keepassxc/`) is reported as a
missing or unverified setting.

### F7. Development tool versions exceed project pins (confirmed)

`~/.venvs/tools` on `beta` has Black 26.10.1 and Sphinx 9.2.0, while
`pyproject.toml` pins `black>=25,<26` and `sphinx>=8,<9`. Black 26 would
reformat 19 files at `db06c97`, so `black --check` fails on unchanged
sources; the documentation tests pass under Sphinx 9.2. Decide whether to
raise the pins (and reformat in a separate commit) or install pinned
versions in an isolated environment.

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
