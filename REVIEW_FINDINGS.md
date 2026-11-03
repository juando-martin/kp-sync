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

## Scope

Read closely (at `db06c97`, re-checked at `v0.2.0`): file and drive
endpoints, the engine's observation, write and commit path, baseline
publication, `cleanup.py`, `services.py` daemon start/stop, short status
hashes, the kp-access bridge and operations, session `authorized_keys`
editing and `sftp-serve` confinement, credential caching and the rclone
endpoint.

Not yet reviewed in detail: code new in `v0.2.0` (`drive-sync`,
`rclone_credentials.py`, the daemon credential watch and draining, and
`service install|enable|disable|uninstall daemon`), plus `daemon/server.py`,
`merge.py`, `sessions/session.py`, `config.py`, `integration.py` and the UI
modules.

Test run on `beta` at `v0.2.0`: 443 tests, 3 failures and 9 errors, all
from F1–F3 (environmental, not product defects).

## Test isolation

### F1. Status tests can activate the production daemon (confirmed)

`Client.__init__` calls `ensure_run_dir(config.profile)`
(`src/kp_sync/daemon/client.py:56`), which reads the process
`XDG_RUNTIME_DIR` even when the test's `Config` was loaded with a temporary
environment. `tests/test_status.py` patches `kp_sync.cli.ensure_run_dir`
but not the client:

- `test_live_status_includes_leases_and_uses_daemon_exit_status` creates the
  real `$XDG_RUNTIME_DIR/kp-sync/` (observed on `beta` at both revisions).
- `test_stale_socket_uses_cached_status_without_starting_daemon` also leaves
  `Client.request` unpatched, so it connects to the real
  `$XDG_RUNTIME_DIR/kp-sync/control.sock`. On `alpha` that is the production
  socket unit; connecting starts the daemon by socket activation, whatever
  `activate=False` says.

Fix: pass the runtime directory into `Client` (or derive it from the
config's environment), patch `XDG_RUNTIME_DIR` in these tests as
`tests/test_access_ops.py` does, and assert the client directory is the
isolated one.

### F2. Tests depend on the checkout location and umask (confirmed)

- `tests/test_integration.py:30` and `tests/test_desktop.py:74` use
  `tempfile.TemporaryDirectory(dir=Path.cwd())`. That writes test data into
  the repository (permanent storage) and makes results depend on the
  checkout's ancestors. On `beta`, `~/workspace` is mode 775, so the
  install-safety check rejects the path and seven integration tests fail
  ("installation path is group- or world-writable").
- Four uninstall tests in `tests/test_services.py` create fixture unit and
  desktop files with `write_text`, so their mode follows the umask. On
  `beta` the umask is `0002`, the files come out 0664, and uninstall
  rightly refuses them ("unsafe file permissions or owner"). Product code
  is unaffected: `paths.atomic_write` sets 0600 explicitly.

Fix: create fixtures under runtime storage (`TMPDIR`) with a private 0700
root, and give fixture files explicit modes (or set a known umask in
`setUp`).

### F3. Compatibility test assumes KeePassXC 2.6 `import` (confirmed)

`tests/test_compatibility.py:53` runs `keepassxc-cli import` without
`-p`. KeePassXC 2.7.10 then fails with "No key is set. Aborting database
creation." With `-p` the round trip and merge test passes. Product code is
unaffected: `verify.py` uses `db-info --quiet` with the password on stdin,
which works on 2.7.10 and rejects a wrong password. Fix: pass `-p`, which
2.6.6 should also accept; confirm on `alpha`.

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
