# Changes

User-visible changes per release, newest first. Commit history and tags
hold the details; design decisions live in the `design/` chapters.

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

- Agent session instructions moved from `.github/copilot-instructions.md`
  to `CLAUDE.md`.
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
