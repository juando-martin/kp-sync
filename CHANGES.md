# Changes

User-visible changes per release, newest first. Commit history and tags
hold the details; design decisions live in the `design/` chapters.

## Unreleased

### Changed

- Cloud remotes that list other content hashes no longer download the
  database on every sync. Dropbox's content hash is now checked against
  local bytes like MD5 and SHA-256, so a Dropbox cloud needs one listing
  for an unchanged sync, as Google Drive does. Other hashes of at least
  64 bits that kp-sync cannot compute (such as OneDrive's `quickxor`)
  show a copy unchanged, which avoids downloads on unchanged syncs and
  before writes; the copy is still downloaded after a write to confirm
  it.

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
