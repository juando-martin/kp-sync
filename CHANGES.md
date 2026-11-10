# Changes

User-visible changes per release, newest first. Commit history and tags
hold the details; design decisions live in the `design/` chapters.

## 0.5.1 (2026-11-10)

### Added

- `kp-sync completion install [--alias NAME]` writes Bash completion for
  kp-sync and for aliases such as `kps` into bash-completion's per-user
  directory; no profile needed.

### Changed

- The hourly idle sync (`kp-sync sync --idle`, run by the timer) is
  skipped while every cloud is clearly unreachable (its reach check
  fails or its credentials are not published), without starting the
  daemon; one still runs every eight intervals. Idle syncs no longer
  fail because a copy is unreachable, so the timer's unit stops failing
  every hour offline. Reinstall the units (`kp-sync service install
  daemon`, done by `tools/releases.py install`) to get `--idle`.

### Fixed

- Notices from a sync run by the daemon (a decommissioned or damaged
  key drive, a stale-cache save, a rejected password) now appear as
  desktop notifications while `[ui] notify` is true; they were only
  logged.

## 0.5.0 (2026-11-06)

### Added

- `kp-sync drive-retire DRIVE` decommissions a key drive, present or
  not, named by its udevmon device, a `[drives.aliases]` name or
  `SERIAL:FS_UUID`. Without `--confirm` it previews the drive's record
  and anything that blocks it (attached, unsynced or the only copy of
  a version); applying writes a reversible marker to the shared
  history, so every machine leaves the drive untouched from its next
  sync, and removes this machine's udevmon device (`--keep-device`
  sets it to ignore). `drive-setup` enrols the drive again. The first
  marker moves the shared history to schema 3, which kp-sync before
  0.5.0 cannot read: upgrade every machine before using it.
- Before a run's first rclone call, a check of at most `reach_timeout`
  seconds (default 3) tries a TCP connection to the cloud's server
  (derived from the rclone remote, or a proxy) and asks NetworkManager
  about a captive portal. When it fails, the cloud is skipped at once,
  so an offline start shows the "cloud cannot be reached" warning
  before KeePassXC opens instead of launching silently after 20 s.
  `reach_check = "HOST:PORT"` or `false` per `rclone` endpoint;
  `doctor` reports a check that fails while rclone works.
- A pre-launch sync still running when the application starts and then
  ending without the cloud shows the same warning late.

### Fixed

- An attached key drive without a database (as left by `drive-setup`
  on an empty stick) is now filled with the current version; it was
  skipped as unavailable.
- `drive-setup` gives a second profile's drives and actions their own
  tag (`keepass-PROFILE`), so one profile's drives no longer run
  another profile's actions, and describes the trigger by the
  profile's existing actions.
- After the first rclone timeout of a run the cloud is unavailable for
  the rest of it, so a hanging cloud costs one timeout per run instead
  of one per call.

## 0.4.0 (2026-11-06)

### Added

- `kp-sync drive-setup MOUNTPOINT NAME` prepares a plugged key drive
  (udevmon 0.7.0+): it registers the drive and this profile's actions
  in kp-sync's udevmon drop-in and runs them, so the normal attach
  fills an empty drive with the current database and history (a drive
  that already holds one is merged, never overwritten). `--dry-run`
  shows the plan.

- Once per attachment, a key drive's history bases are checked against
  their SHA-256 names; a damaged one is moved to `damaged/` on the
  drive (never deleted), replaced from a verified copy in the same run,
  and reported, so a failing stick is noticed before it is the only
  offline copy.

- When the pre-launch sync cannot reach the cloud, a warning dialog says
  so and how long it has been since the cloud was last reached (with
  `confirm_without_cloud`, the same text is a question). `status` shows
  when each cloud was last reached from this machine, and a notification
  appears when hub changes have waited longer than
  `[daemon] cloud_warning_hours` (default 24) for the cloud.

## 0.3.8 (2026-11-06)

### Changed

- The udevmon examples are now drop-ins for udevmon 0.7.0 or later
  (`~/.config/udevmon/config.d/50-kp-sync.toml`), with
  `notify_on_failure`; the rclone provider's own drop-in orders its
  actions around kp-sync's with `order` 10/90. `doctor`'s integration
  check reads udevmon's drop-ins too.

### Added

- `tools/run_tests.py` runs the test suite in parallel, one process per
  module (about 40 s instead of 80 s on a 4-core laptop);
  `tools/releases.py` uses it for the full suite. Test databases use a
  light key derivation, which took the serial suite from about 6.5
  minutes to 80 s.

## 0.3.7 (2026-11-05)

### Added

- `doctor` checks that every `kp-sync` call in udevmon's actions and in
  kp-access's `file-op-cmd` selects an existing profile, and that
  kp-access's `db-path` is this profile's staged copy (both had to be
  found by hand when `alpha` moved to its own profile).

## 0.3.6 (2026-11-05)

### Changed

- Without `~/.config/kp-sync/config.toml`, kp-sync stops and lists the
  named profiles when any exist, instead of silently running on built-in
  defaults that reach nothing. Pass `--profile NAME`.

### Documentation

- The README explains installing Bash completion when
  `register-python-argcomplete` is not on `PATH`, and
  `examples/bash-completion/kps` completes an alias such as
  `kps='kp-sync --profile HOST'`.

### Fixed

- `tools/releases.py install` builds in pip's isolated environment when
  the target environment has no setuptools, instead of failing with
  `Cannot import 'setuptools.build_meta'`.

## 0.3.5 (2026-11-05)

### Fixed

- `service uninstall daemon` and `service disable daemon --now` no
  longer refuse a clean daemon stop with "could not verify shutdown
  result" (systemd drops the exit record of a stopped unit that nothing
  references).

### Added

- `service install daemon` checks every target directory and that
  systemd can start the `kp-sync` it names before writing anything, and
  reports all problems at once; `doctor` checks the installed daemon
  unit's program too.
- `status` shows when this machine's hub was saved since the last sync
  ("its changes are not synchronized yet"), checked locally without
  contacting the cloud, so offline edits are visible.
- `tools/releases.py` cuts a release (`release`), installs a tag into
  an environment and refreshes the profile's daemon (`install`), and
  tests a tag from an export with a Markdown report (`test`).
- The daemon logs a warning when `up`/`down` (main-mode drive events,
  which start and quit applications) reach a profile with a persistent
  hub; use `drive-sync --mode secondary` there.

## 0.3.4 (2026-11-05)

### Fixed

- `kp-sync service install daemon` writes the absolute path of the
  running `kp-sync` into units and desktop entries. A bare name failed
  to start (`status=203/EXEC`) when kp-sync lived outside systemd's fixed
  search path, for example in `~/.local/bin` or a virtual environment.

## 0.3.3 (2026-11-05)

### Added

- `require_encrypted` accepts a persistent hub on a ZFS dataset with
  native encryption and its key loaded, as well as on dm-crypt/LUKS.

### Changed

- `rclone_transport` now defaults to `"auto"`: the daemon keeps a
  persistent `rclone rcd` per rclone endpoint (rclone 1.61 or later) and
  falls back to plain commands otherwise. Set `"cli"` to keep separate
  rclone processes.

## 0.3.2 (2026-11-04)

### Added

- `--debug`, or `KP_SYNC_DEBUG=1` in the environment (for example on the
  daemon's unit), prints `DEBUG:` lines explaining what a run decided:
  each copy's state with short hashes and how it was observed, sync lock
  waits, restarts, the outcome, daemon requests and queueing, and the
  duration of configured commands and the `keepassxc-cli` check, and the
  command line of every external program run (dialog text is not shown).
  It never shows passwords, stdin or command output. A command handled
  by the daemon notes that its `--debug` does not reach the daemon.

### Changed

- Commands run from a terminal no longer print routine `INFO:` lines
  such as rclone timings and credential-copy housekeeping; `--verbose`
  shows them again. Results, warnings and errors are unchanged, and the
  daemon, udevmon hooks and kp-access operations log exactly as before.

### Documentation

- The `kp-sync(1)` and `kp-sync-config(5)` man pages now describe every
  command, option and configuration key with its type, default and the
  meaning of each accepted value. `kp-sync(1)` adds the missing
  `repair-history` and `sftp-serve` commands, the copy states printed by
  `sync`, and environment and file details.

## 0.3.1 (2026-11-04)

### Changed

- Cloud remotes that list other content hashes no longer download the
  database on every sync. Dropbox's content hash is now checked against
  local bytes like MD5 and SHA-256, so a Dropbox cloud needs one listing
  for an unchanged sync, as Google Drive does. Other hashes of at least
  64 bits that kp-sync cannot compute (such as OneDrive's `quickxor`)
  show a copy unchanged, which avoids downloads on unchanged syncs and
  before writes; the copy is still downloaded after a write to confirm
  it.

### Documentation

- Example profiles under `examples/configs/` (cloud only, rcd transport,
  Dropbox, pendrives only, persistent hub with folders, every option) and
  udevmon actions for main and secondary drive modes under
  `examples/udevmon/`, with an index in `examples/README.md`. A test keeps
  them loadable and their commands valid.

## 0.3.0 (2026-11-04)

### Added

- Optional `rclone_transport = "auto"` on an `rclone` endpoint: the
  daemon keeps a persistent `rclone rcd` on a private socket, so cloud
  calls skip repeated authentication (about 1 s instead of 2–4 s per call
  on Google Drive). It needs rclone 1.61 or later; older rclone, other
  processes when the daemon is not running and custom commands keep
  using plain rclone commands. The default stays `"cli"`.

### Changed

- Cloud history is faster to publish: all history copies (bases) a sync
  uploads, downloads or prunes go to the cloud in one rclone call per
  direction instead of one call per file (each call costs several
  seconds on Google Drive). `state.json` and the database are still
  written separately and in order.

### Fixed

- A copy is no longer taken as unchanged just because its size and
  modification time are unchanged. kp-sync skips downloading a copy only
  when its listing or probe includes a content hash (SHA-256, SHA-1 or
  MD5, as Google Drive provides); other rclone remotes and `command`
  endpoints are downloaded on every sync. A `command` endpoint whose
  `probe` prints a real content hash can set the new
  `probe_is_content_hash = true` to keep the shortcut.
- A pendrive whose history cannot be updated (full, read-only remount,
  I/O error) no longer fails every sync while it is attached. Once the
  cloud (or the first drive when offline) holds the new history, the
  failure is logged and kp-sync asks whether to continue without updating
  that pendrive. Accepting is remembered until the drive's history is
  next updated; declining, or a background sync without a dialog, leaves
  a pending decision (`kp-sync resolve` asks again). Launching, kp-access
  and ejecting the drive are not blocked.
- `kp-sync history` and `kp-sync doctor` no longer race the daemon over
  the private rclone credentials copy. Importing a new provider config or
  removing the copy after the key drive leaves now waits for any rclone
  run of the profile, in any process, so a running rclone can no longer
  overwrite a newly imported login or recreate a removed copy.
- A pendrive whose database copy cannot be written (read-only remount,
  full, I/O error) no longer fails every sync. If the copy is verified
  unchanged after the failed write, that drive is skipped like an
  unreachable one, the other copies are synchronized, and kp-sync asks
  whether to continue without updating it (the same remembered answer
  and pending decision as for pendrive history). A copy changed by the
  failed write still stops the sync.
- After a failed sync the daemon no longer starts a new visible sync,
  with its progress dialog, every few seconds. It retries on the next
  save, hub change or background poll.
- The KeePassXC "reload when modified externally" check (daemon and
  `kp-sync doctor`) also finds KeePassXC's settings under a custom
  `XDG_CONFIG_HOME` and in a Flatpak installation, instead of reporting
  the setting as missing.
- A saved history copy (base) that disappears from the cloud or a
  pendrive, for example because two machines pruned at the same time or
  it was deleted by hand, is uploaded again from wherever a copy still
  exists: the runtime cache, another history store or a database copy
  seen in the same sync. After a reboot only the missing ones are
  fetched. If a version's copy exists nowhere, kp-sync refuses to publish
  new history and shows a dialog naming the affected version and the
  repair command, which you must accept (otherwise it stays a pending
  decision); the database copies keep synchronizing meanwhile.
- New `kp-sync repair-history`: previews versions whose saved copies are
  missing and whether they can be recovered; `--apply
  --confirm-writers-paused` restores what it can and removes versions
  whose copy exists nowhere, keeping the remaining history connected.

## 0.2.1 (2026-11-03)

Maintenance release: test isolation fixes found by the code review on the
development laptop. No configuration or behaviour changes for users.

### Fixed

- `kp-sync status` queries the daemon socket in the runtime directory it
  has just checked, instead of re-deriving the path from the process
  environment. `daemon.client.Client` accepts an explicit `directory`.

### Tests

- Status tests no longer create the real `$XDG_RUNTIME_DIR/kp-sync/` or
  connect to the real control socket, which socket activation would turn
  into a production daemon start.
- Integration and desktop tests create their temporary trees on runtime
  storage (`tests.fixtures.private_tempdir()`), not inside the checkout,
  so they pass when the checkout's ancestors are group-writable.
- Install and uninstall tests no longer depend on the user's umask.
- The KeePassXC compatibility test passes `-p` to `keepassxc-cli import`
  only when the installed version supports it (2.7 and later), so it runs
  on both 2.6 and 2.7.

### Documentation

- Agent session instructions moved to `CLAUDE.md`.
- Open code-review findings are tracked in `REVIEW_FINDINGS.md`;
  `NEXT_STEPS.md` records the development host.

## 0.2.0 (2026-11-03)

Separates drive attachment from rclone credential provisioning and makes
daemon installation and activation explicit.

### Added

- `kp-sync drive-sync up|down --mode main|secondary --drive-id
  SERIAL:FS_UUID [MOUNTPOINT]` for udevmon drive actions. `--mode` is
  required: `main` sends `up`/`down`, `secondary` sends `attach`/`detach`.
  It verifies the mounted drive's identity, writes the pending-up marker
  under the shared `keydisk.lock`, submits the request with `--no-wait`,
  and never touches rclone credentials.
- Private rclone credentials: an rclone endpoint's `rclone_config` now
  names a provider-owned file that kp-sync only reads. Each profile gives
  rclone its own copy under `<profile run dir>/rclone/<endpoint>.conf`,
  imports changes between rclone runs, and drains the copy after the
  provider removes the file (one more sync, then deletion). Without an
  external file or copy the cloud endpoint reports "credentials
  unavailable" and is skipped. Endpoints without `rclone_config` are
  unchanged.
- The daemon watches provider configs: a new or changed file triggers a
  sync while leases are held; a removed one triggers the draining sync.
- `kp-sync service install daemon [--desktop]` writes unit files (and
  desktop entries only with `--desktop`) without enabling anything.
- `kp-sync service enable|disable daemon [--now]` manages activation
  triggers; `--now` also starts or stops them.
- `kp-sync service uninstall daemon [--desktop]` removes only unmodified
  generated files for the selected profile, after stopping triggers and
  the daemon; shared templates, other profiles and runtime recovery data
  are kept.
- `kp-sync service status daemon` also reports each trigger's active and
  enabled state.

### Changed

- `doctor` checks rclone through the private copy.
- Runtime cleanup deletes this profile's private credential copies; it no
  longer deletes or inspects a shared runtime rclone config.

### Removed

- `kp-sync install-units` (no alias). Use `service install daemon`, then
  `service enable daemon [--now]`.
- `examples/keydisk-kp-sync.py` and the second-laptop bundle's
  `keydisk-second-laptop` wrapper; udevmon actions call `drive-sync`
  directly. `~/.dynamic_config` is no longer created or read.

### Upgrade notes

- Replace udevmon actions that ran `keydisk.py` with `kp-sync drive-sync`
  actions plus the udevmon rclone-config provider, ordered as described in
  `design/endpoints-and-access.md`.
- Point each rclone endpoint's `rclone_config` at the provider's
  published file, normally `$XDG_RUNTIME_DIR/rclone/rclone.conf`.
- Update scripts that called `install-units`.

## 0.1.3 (2026-10-29)

Maintenance commands and offline pendrive history.

- Collision-aware short hashes in `kp-sync status` (`--long` for full).
- `kp-sync cleanup` previews obsolete baseline files and deletes them only
  with an explicit scope (`--store NAME --confirm-writers-paused` or
  `--runtime`).
- `kp-sync service start|stop|restart|status daemon`.
- Reconciled history and retained bases stay on configured writable
  pendrives while the cloud is available, so a cold offline start can
  recognize their copies.

Earlier releases: see the `v0.1.0`–`v0.1.2` tags and Git history.
