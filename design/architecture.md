# Architecture and safety

[Design index](../DESIGN.md)

Integrated synchronisation of a KeePass database (`pass.kdbx`) between the
laptop, a pendrive, Google Drive and remote systems (sshfs sessions, Windows
via `kp-access`). The laptop is the central place for manipulating the
database. Other personal machines (for example a second laptop keeping a
copy on its encrypted disk) run their own `kp-sync` instance against the
same cloud copy and pendrives.

Status: design only. Implementation is a later phase.

## Goals

- Detect which copies of the database changed since the last sync.
- Changed in exactly one place: promote that copy to all other locations.
- Changed in several places: merge (password and key file are requested).
- Locations are configurable, with any number of endpoints (from only the
  cloud to cloud + pendrive + remote hosts); remote hosts and the pendrive are
  discovered at runtime.
- Several machines, each with its own hub, and several pendrives (all
  mounting at the same path, possibly holding outdated copies) sync safely
  with each other (see "Machines and pendrives").
- Run only on demand: a daemon starts when an application, a pendrive, a
  remote session or kp-access needs the database. Applications and
  kp-access pre-sync; a remote session snapshots the current local hub
  directly. The daemon watches while they run and syncs at their end, then
  exits when nothing needs it (see "Daemon and leases").
- Never write anything to a machine's permanent storage (working state,
  backups and temporary copies live in `$XDG_RUNTIME_DIR`). Exceptions: the
  sync history persists where the database itself already lives (the
  cloud and attached configured pendrives for offline use; see "Baseline
  store"), a machine may explicitly keep its hub on an encrypted disk
  (`hub_persistent`), and sshfs sessions add a restricted, expiring
  public-key line to `~/.ssh/authorized_keys` (see "Remote hosts").
- Replace `keepassdb_ops.sh` and `kp-ssh-access.sh` with feature parity (see
  "Feature parity"); reuse `kp-access`, `keyctl` and `udevmon` unchanged;
  replace the 2-way `kdbx-merge-review` logic with a 3-way merge engine.

## Existing pieces

| Piece | Role today | Role in the new design |
|---|---|---|
| `bin-scripts/keepassdb_ops.sh` | laptop/pendrive/cloud sync (newest mtime wins), launches or raises KeePassXC (or legacy KeePass), inotify watcher, and `kp-access-file-op` mode (the `--file-op-cmd` of `kp-access`: ops dir, `GET`/`PUT`/`DELETE`/`MOVE`, `check_and_sync`) | replaced by the `kp-sync` daemon and its `run` wrapper; `~/bin/keepass` stays as a thin wrapper; the file-op mode becomes `kp-sync access-op` |
| `bin-scripts/kp-ssh-access.sh` | sshfs session over a reverse tunnel, ro/rw, copy back on exit (mtime check) | replaced by `kp-sync session` |
| `kp-access` | HTTP DB + SMB key file for Windows, per-request approval for `GET`; `file-ops` feature (`PUT`/`DELETE`/`MOVE`, authenticated users only, gated solely by `--file-op-cmd`) | reused unchanged; `--file-op-cmd` points at `kp-sync access-op` |
| `keepass-attr` (`kdbx-merge-review`) | interactive 2-way UUID merge (every difference is a conflict, never deletes, standard fields + custom properties only) | conflict UI and entry helpers reused in `kp_sync/merge.py`; the merge itself is a new 3-way engine; the original package stays (`kdbx-url-sync` is unrelated) |
| `keyctl` | ctypes wrapper of the kernel keyring | password cache |
| `udevmon` + `kp-sync drive-sync` actions | starts/stops KeePassXC when a KEYDRIVE pendrive appears/goes (three drives, told apart by serial, all labelled `KEYDRIVE` and mounted at the same path; action `lock = "keydisk"`, `timeout = 120`) | sends `kp-sync up` / `down` on the main laptop, `kp-sync attach` / `detach` on other machines, as requests to the daemon that return at once, always passing the drive identity |

## Packaging decisions

| Component | Decision |
|---|---|
| `keyctl`, `udevmon` | unchanged, used as dependencies/integrations |
| daemon | `kp-sync daemon` as a systemd user service with socket activation and a path unit on the hub; `kp-sync service install daemon --desktop` writes units and desktop entries, then `service enable daemon --now` enables/starts triggers |
| merge | 3-way engine and review model in `kp_sync/merge.py` (pykeepass), reusing field diff, masking and conflict prompts of `kdbx-merge-review`; review is presented through `kp_sync/ui/`, in a small tkinter window or the terminal |
| `kdbx-url-sync` | stays in `keepass-attr`, not part of this project |
| `kp-access` | stays a separate package and needs **no change**: its existing `file-ops` feature already provides uploads; `kp-sync access-op` is its `--file-op-cmd` |
| remote hosts | nothing installed beyond `sshfs` (as today); no remote helper. Locally, a restricted, expiring line per session in `~/.ssh/authorized_keys` and `bwrap` confine sessions; `sshd_config` is not changed |
| Python environment | the shared venv `~/.venvs/tools` (Python 3.13, `include-system-site-packages = false`; already holds `keyctl` and `kp-access`); `kp-sync` is installed there with `pip install -e .` and depends on `pykeepass` (brings `lxml`), `keyctl` and `argcomplete`; TOML uses stdlib `tomllib`. Units and the menu entry call `~/.venvs/tools/bin/kp-sync` by absolute path. Optional docs/dev tools belong in a separate environment. |

## Concepts

- **Hub**: the copy on the machine running `kp-sync`; all merging happens
  there. Each machine has its own hub. The default is
  `$XDG_RUNTIME_DIR/pass.kdbx`; the deployed main-laptop profile explicitly
  uses `$XDG_RUNTIME_DIR/kp/pass.kdbx` (lost on reboot, refilled from
  endpoints). On a machine with `hub_persistent = true` it is a file on
  permanent storage (e.g. `~/data/pass.kdbx` on an encrypted disk), opened
  directly by KeePassXC and never deleted by `kp-sync`. With
  `require_encrypted = true` (default for persistent hubs) `kp-sync`
  refuses to run if that file is not on a dm-crypt/LUKS device (checked
  with `lsblk`).
- **Endpoint**: any other location holding a copy. The engine knows only the
  generic endpoint interface (`fetch`, `store`, `probe`, `available`,
  `readonly`, `identity`); the number and kind of endpoints is entirely up to
  the configuration, from zero (hub only) to many. `probe` returns a cheap
  fingerprint (hash or size+mtime) without downloading, when the type allows
  it. Endpoint types:
  - `file`: a path, optionally resolved at runtime. The pendrive is a `file`
    endpoint with `source = "runtime"`: each attached drive is a separate
    instance, created by `kp-sync attach` (or `up`) with its mount point
    and identity, and registered in `drives/`. Several drives can be
    attached at once (e.g. `KEYDRIVE` and `KEYDRIVE1`). The `identity` is
    `SERIAL:FS_UUID` (drive serial plus filesystem UUID, both supplied by
    udevmon as `{drive_serial}` and `{fs_uuid}`, or looked up with `lsblk` from
    the mount point). The mount path means nothing, since all drives mount
    at the same place, and the serial distinguishes sticks cloned with the
    same filesystem UUID. History is kept per identity.
  - `rclone`: commands are configurable (argument lists, no shell). Default
    `rclone copyto REMOTE:pass.kdbx FILE` (download) and
    `rclone copyto FILE REMOTE:pass.kdbx` (upload), as in the current script,
    which updates the file in place and so keeps Google Drive's version
    history. `probe` uses `rclone lsjson --hash REMOTE:pass.kdbx`; more than
    one object with that name (Drive allows duplicates) is an error to be
    resolved by hand. The rclone config may come from the pendrive
    (`$XDG_RUNTIME_DIR/rclone`) or from a normal path.
  - `command`: arbitrary fetch/store (and optional probe) commands.
  - `session`: a remote system with the database staged for it (sshfs
    read-only/read-write, or Windows via `kp-access`), discovered from
    `$XDG_RUNTIME_DIR/kp-sync/sessions/`; enabled with `[sessions]`.
  Each endpoint has `enabled`, `readonly`, `optional` (absence is silent
  rather than reported) and `on_unavailable` (`skip`, `warn`, `fail`).
- **Profiles**: with one hub and N endpoints the algorithm is identical for
  any N. Example: a laptop with only a local copy and the cloud is a hub plus
  one `rclone` endpoint, with no pendrive and no sessions; commands that
  refer to an unconfigured endpoint (`up MOUNTPOINT`, `session`) report so
  instead of failing obscurely. A machine selects its set of endpoints with
  `--config FILE` or `--profile NAME` (`~/.config/kp-sync/NAME.toml`). A
  second laptop is typically a persistent hub plus the cloud plus runtime
  pendrives, with no sessions and no kp-access.
- **Version history** (replaces per-endpoint baselines). Every commit
  creates a **version**: the SHA-256 of the committed file, its parent
  version(s) (two or more after a merge), a `generation`, the host and the
  time. The file itself is kept as `bases/<sha>.kdbx`. Separately, every
  **copy** records the version it last held after a sync. A copy is a
  physical location, keyed independently of the machine that synced it:
  `cloud:REMOTE`, `drive:SERIAL:FS_UUID`, `hub:HOST`, `session:HOST:ID`.
  History and copy records together form `state.json`, which is shared by
  all machines through the baseline store (see below).
  - The ancestor for a 3-way merge between two copies is the newest common
    ancestor of their versions in the history (like `git merge-base`),
    whichever machine created those versions. A pendrive last synced on
    the second laptop and now plugged into the main laptop is therefore
    merged correctly.
  - A copy whose bytes equal a known version that is an ancestor of the
    current version is simply out of date: it is overwritten (after a
    backup) without merge or password. This covers most outdated pendrives.
- **Endpoint states** (computed per run, for the hub too):
  - `unchanged`: its bytes are the current version.
  - `stale`: its bytes are an older known version (an ancestor of the
    current one); it is a target, never a source.
  - `diverged`: its bytes are a known version that is neither the current
    version nor one of its ancestors (another branch of the history, for
    example committed by a machine that only reached a pendrive store
    while another one committed to the cloud). It is merged with the
    current version using their merge-base, never overwritten.
  - `changed`: unknown bytes, and the copy has a record; the version in that
    record is where it diverged from. For an endpoint with
    `foreign_writers` (the cloud: phones and other KeePass apps write to
    it without `kp-sync`), that is only an assumption: the writer may have
    started from an older cached copy. See "Foreign changes" in the sync
    algorithm.
  - `new`: unknown bytes and no usable record (a drive never seen before,
    or seen so long ago that its version was pruned). It is never promoted
    as the only source; see "Unknown copies" in the sync algorithm.
  - `missing`: unreachable or absent.
  The **current version** is computed from the versions held by reachable
  copies: the **heads** are those that are not an ancestor of another one.
  With one head, it is the current version. With several (branches that
  never met), the one with the highest `generation` (then the latest
  time, then the host name, so every machine picks the same) is the
  current version and the others are `diverged`, so the run merges them.
  If no reachable copy holds a known version, there is no current version
  and all differing copies are merged. A version's `generation` is one
  more than the highest of its parents.
  A missing hub (after a reboot or logout, since `$XDG_RUNTIME_DIR` is
  cleared) is only a target: it is filled from the endpoints, never
  treated as a deletion. When the hub is found missing, its `hub:HOST`
  record is cleared, so a file that later appears there without being
  written by `kp-sync` (for example a new database created by KeePassXC
  on an empty path) is `new`, never `changed`, and cannot be promoted. A
  persistent hub is never missing; if its file disappears, `kp-sync`
  stops and reports instead of refilling it.

## Error handling and safety

- Missing pendrive, unreachable cloud or an absent session: skip, report,
  resync next run (no queue).
- One sync at a time: the daemon serialises syncs and coalesces triggers;
  `--no-daemon` runs take the `lock` flock.
- Temporary copies and key file copies never leave `$XDG_RUNTIME_DIR`
  (a tmpfs; with unencrypted swap it may still reach the disk, which
  `doctor` reports).
- A copy whose bytes are unknown is never promoted only because it is the
  only one that differs (today's "newest mtime wins" would let an old
  pendrive overwrite everything).
- A promotion interrupted on a filesystem without atomic exchange (crash
  or drive pulled between displacing and installing) is recovered from the
  displaced file left next to the target on the next sync that sees that
  endpoint (see step 5 of the sync algorithm).
- Removing a synchronized runtime hub during `down` also needs a race
  check: atomically displace it to a uniquely named runtime backup, then
  hash that displaced file against the synchronized content checked before
  the rename. Remove only a match. Restore a mismatch with
  `RENAME_NOREPLACE`, or retain it alongside a concurrently recreated hub
  and leave the decision pending. Never use a hash-then-unlink sequence
  that could delete a newer atomic save. Retain runtime state while any
  client, application or pending decision still needs it; delete
  only this profile's private rclone copies, never the provider's file.
- Desktop notification (`notify-send`, if available) when a decision is
  pending or an endpoint was skipped; without a display, only the log and
  `status`.
- Logging: stderr (terminal clients), the journal (daemon), plus `log` in
  the run dir; `--debug` replaces `KEEPASSDB_DEBUG`.
- `--dry-run` on `sync` (and `status`) performs steps 1 to 3 only.

## Feature parity with the replaced scripts

- Launch KeePassXC, or raise and move to the current desktop an existing
  window (`wmctrl`); `keepass-legacy` for the old mono KeePass.
- Ask whether to launch when the cloud is unreachable
  (`confirm_without_cloud`).
- Sync progress dialog (zenity pulsate) when a display is present.
- `kp-access-file-op` behaviour (now `kp-sync access-op`).
- sshfs sessions in `ro` and `rw` mode with copy back (now through `sync`).
- The inotify watcher (now the daemon, only while a lease is held).
