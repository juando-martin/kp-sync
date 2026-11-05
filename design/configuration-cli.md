# Configuration and command contracts

[Design index](../DESIGN.md)

## Configuration (TOML)

Searched at `~/.config/kp-sync/config.toml` (overridable with `--config` or
`--profile`). Without that file kp-sync uses built-in defaults, unless
named profiles (`*.toml`) exist beside it: then it stops and asks for
`--profile`, since defaults reach no endpoint and the omission is a
mistake. Keys:

- `hub` (default `$XDG_RUNTIME_DIR/pass.kdbx`), `hub_persistent` (default
  false), `require_encrypted` (default true when `hub_persistent`),
  `host` (default the hostname; the name used in `hub:HOST`), `db_name`
  (default `pass.kdbx`), `key_file` (default `~/data/Personal/pass.keyx`).
- `[[endpoints]]`: a list, one table per endpoint: `name`, `type` (`file`,
  `rclone`, `command`), type-specific keys (`path` or `source = "runtime"`
  and `identity` for `file`, at most one enabled `source = "runtime"`
  endpoint; `remote`, `rclone_config` (the provider-owned file, only read; kp-sync
  passes rclone its own private copy), `download`, `upload`,
  `probe` for `rclone`, whose availability is the `probe` itself run with
  `timeout`, not a ping that may be blocked; `fetch`, `store`, `probe` and
  `probe_is_content_hash` for `command`), and the common `enabled`, `readonly`, `optional`,
  `on_unavailable`, `timeout`, `foreign_writers` (default true for
  `rclone` and for read-write sessions, false otherwise: other programs
  may write this copy, so its changes get the "Foreign changes" check). Commands are argument lists with
  `{file}` / `{remote}` placeholders, never shell strings.
  `probe_is_content_hash` (default false, requires `probe`) declares
  that the `command` probe prints a content hash of the copy. A custom
  probe must then change whenever the content changes: an unchanged
  output is taken as the recorded version without downloading it (see
  "Sync algorithm", step 2). Without it, the copy is fetched every run.
  `rclone` endpoints do not accept it; their listing qualifies by
  itself when it includes a content hash of at least 64 bits (see
  "Sync algorithm", step 2).
  `rclone_transport` (`rclone` only): `"auto"` (default) uses the
  daemon's persistent `rclone rcd` when this rclone supports it (1.61+)
  and commands otherwise; `"cli"` always runs rclone commands (see "Optional rcd
  transport" in the endpoints chapter).
- `[sessions]`: `enabled`, `mount_dir` (remote default `~/kp`),
  `tunnel_port` (default 2222; the remote end of the reverse tunnel, so
  one session per remote host at a time: a port already in use there
  fails the session with a clear error), `default_mode` (`ro`), `session_key_ttl`
  (default 24 h, the `expiry-time` of a session's `authorized_keys` line).
- `[ssh_sessions.NAME]`: named managed session settings: required `target`
  (`[USER@]HOST[:PATH]`), optional `mode` (`ro` or `rw`, defaults to
  `sessions.default_mode`), `mount_dir` (defaults to `sessions.mount_dir`),
  and `unconfined` (default false). Manage it with
  `kp-sync service start|stop|status ssh-session NAME`.
- `[access_owners.NAME]`: required `command` argv array for a named
  foreground `kp-access-owner` process. Manage it with
  `kp-sync service start|stop|status access-owner NAME`; the child runs
  without a shell, and its stdout/stderr are discarded so logs contain only
  lifecycle events and errors.
- `[access]`: `staging_dir`, `db_path` (the `--path-name` as seen by
  `kp-access`, normalized and absolute). A directory component is preferred
  because it narrows the server's file-operation scope. A legacy root-level
  path such as `/pass.kdbx` is also supported: `kp-sync` independently
  restricts operations to that exact database name and its temporary siblings.
- `[credentials]`: `cache_timeout` (default 15 min).
- `[credentials.keepassxc]`: required `url` opts into browser-protocol
  password retrieval from an unlocked KeePassXC database. The URL is a
  local lookup identifier, never fetched. No enable flag or password is
  stored in configuration. See "Credentials".
- `[ui]`: `mode` (`auto`, `tty`, `gui`, `none`; default `auto`), `gui`
  (`zenity` default, `kdialog`, or a custom command), `ask_first` (default
  true), `dialog_timeout` (default 10 min), `notify` (default true),
  `progress` (default true). `gui` may be a backend name, a custom command
  executable string, or a non-empty argument array; commands are never run
  through a shell. See "User interaction".
- `[baseline]`: `stores` (endpoint names in priority order, default
  `["cloud", "pendrive"]`; explicit names are checked as described in
  "Baseline store"), `dir` (default `kp-sync`), `keep_days` (default 180).
- `[drives]`: `notify_stale` (default true: notify when an attached drive
  held an outdated copy and was updated); `directory` (default `.`, the
  relative database directory on runtime-discovered drives). The database
  is `<mountpoint>/<directory>/<db_name>`; absolute paths, parent traversal
  and paths resolving outside the drive are rejected. This does not move
  the drive baseline store (`<mountpoint>/<baseline.dir>`) or emergency
  rescue files at the drive root.
  A nested layout can use `hub = "/run/user/UID/kp/pass.kdbx"`,
  `[drives] directory = "kp"` and a cloud endpoint with
  `remote = "GoogleDrive:kp"`. Cloud history is relative to that remote
  root (`GoogleDrive:kp/kp-sync` with the default `baseline.dir`).
  `[drives.aliases]` is an optional per-profile mapping from the complete
  `SERIAL:FS_UUID` physical identity to a human-readable display name:

  ```toml
  [drives.aliases]
  "1111222233334444:AB12-CD34" = "Daily backup"
  "5555666677778888:AB12-CD34" = "Spare backup"
  ```

  Aliases are display-only: they do not change endpoint identity, history,
  authorization, mount matching or drive selection. Do not key them by
  mount path or filesystem label. Validate identity keys and require
  non-empty names without terminal control characters. Unconfigured
  drives retain their technical identity. Duplicate aliases are allowed;
  always show the technical identity on a secondary line to disambiguate.
  Labels are local profile configuration, not shared-history metadata.
  Aliases are validated as complete drive identities and non-empty display
  strings without terminal control characters. Configuration does not
  migrate databases, old copies or history.
- `[backups]`: `keep`.
- `[verify]`: `max_entry_drop` (default 10 %) and `min_entry_drop`
  (default 5 entries): a candidate that loses more than both, relative to
  the version it diverged from, needs confirmation; `foreign_check` (`always`, default: every foreign
  change is checked, which needs the password; `merge-only`: checked only
  when another copy also changed, so a lone phone edit is promoted without
  password, accepting the risk of a stale-cache save).
- `[merge]`: `engine` (`kp-sync` 3-way, default; or `keepassxc` for
  `keepassxc-cli merge`, newest wins, no review; with KeePassXC 2.6.6 it
  did not apply a deletion recorded in the other copy, so deleted entries
  can come back), `review` (`interactive`,
  `auto-hub`, `auto-other`, `defer`).
- `[daemon]`: `debounce` (default 2 s), `poll_interval` (default 15 min;
  how often endpoints are probed while a lease is held, mainly to notice
  cloud edits from phones; 0 disables), `idle_exit` (default 2 min),
  `prestart_timeout` (default 20 s),
  `access_lease_ttl` (default 1 h), `hub_path_unit` (default true),
  `idle_sync` (default true for a persistent hub, false otherwise),
  `idle_sync_interval` (default 1 h), `rescue_wait` (default 60 s),
  `cloud_warning_hours` (default 24; 0 disables): notify when the hub has
  changes not yet synchronized and the cloud has not been reached for
  that long, at most once per that period (see Lifecycle).
- `[sync]`: `lock_timeout` (default 60 s, for `--no-daemon` runs),
  `max_restarts` (default 3).
- `[launch]`: `default_app` (`keepassxc`), `confirm_without_cloud`
  (default false: when the pre-sync cannot reach the cloud, a warning
  dialog appears beside the application; true: ask before launching),
  `quit_on_last_drive` (default true on the main laptop).
- `[apps.NAME]`: `command` (argument list), `process` (process names to
  track), `reloads` (reloads the file by itself when changed on disk),
  `single_instance`. `keepassxc` and `keepass-legacy` are predefined.

Example for a second laptop:

```toml
hub = "~/data/pass.kdbx"
hub_persistent = true
key_file = "~/data/pass.keyx"

[[endpoints]]
name = "cloud"
type = "rclone"
remote = "GoogleDrive"

[[endpoints]]
name = "pendrive"
type = "file"
source = "runtime"
```

## Commands

| Command | Purpose |
|---|---|
| `kp-sync --version` | print the package version and exit without loading configuration or contacting the daemon/endpoints |
| `kp-sync daemon` | the daemon (started by `kp-sync.service`) |
| `kp-sync run [--app NAME] [-- ARGS ...]` | pre-sync, start the application (or raise it if running) and hold a lease for its life; used by `~/bin/keepass`, the menu entry and autostart |
| `kp-sync up [--drive-id ID] [MOUNTPOINT]` | `attach` (if a mount point is given), then `run` the default application; idempotent (udevmon on the main laptop) |
| `kp-sync attach [--drive-id SERIAL:FS_UUID] MOUNTPOINT` | register the drive (identity looked up with `lsblk` if not given), take its lease and sync the hub, the cloud and it (udevmon on other machines) |
| `kp-sync drive-setup [--rclone] [--dry-run] MOUNTPOINT NAME` | prepare a plugged key drive (udevmon 0.7.0+): check it is mounted, writable and the profile has a runtime pendrive endpoint; register it as udevmon device `NAME` (tags `keepass`, plus `rclone` with `--rclone`) with `udevmon devices add --from-mountpoint ... --file 50-kp-sync`, or reuse the device already matching its identity (adding missing tags); add this profile's `drive-sync` actions to that drop-in if missing (`secondary` for a persistent hub, else `main`; `notify_on_failure`); then `udevmon trigger NAME --wait`. The resulting attach fills an empty drive through the engine's checked install (`RENAME_NOREPLACE`) or merges an existing database like any copy; the command writes no database and edits udevmon only through its own commands. `--dry-run` prints the plan only |
| `kp-sync drive-sync up\|down --mode main\|secondary --drive-id ID [MOUNTPOINT]` | implemented: the udevmon drive action; validates identity, keeps the pending-up marker and submits `up`/`down` (`main`) or `attach`/`detach` (`secondary`) with `--no-wait`; `--profile` and `--mode` are required; never touches rclone credentials (see Integration points). On a profile with a persistent hub the daemon logs a warning for `up`/`down`, which start and quit applications; such machines normally use `secondary` |
| `kp-sync detach [--drive-id ID] [MOUNTPOINT]` | release the drive's lease (the drive is usually gone already) and warn if it missed a due sync |
| `kp-sync sync [--dry-run]` | ask the daemon for a sync (the algorithm below) and wait for its result |
| `kp-sync resolve` | handle pending decisions now, on this terminal or on the desktop |
| `kp-sync eject` | final sync including the pendrive, then unmount it (the clean way to remove it); with `quit_on_last_drive` and no other drive attached, also does what `down` does |
| `kp-sync down [--drive-id ID]` | `detach`; when no drive is left and `quit_on_last_drive` is set (default on the main laptop, as today), quit the applications started by `up` (their leases end, which runs the post-sync), then remove the hub and the run-dir files, including kp-sync's private rclone copy (only after that post-sync, since the cloud needs it; the externally provided config is never removed, see Integration points). A persistent hub is never touched. A hub whose content is not on at least one other copy (reachable now or confirmed by the last commit) is never deleted: it, `pending.json` and the copies a pending merge needs are kept (0600, tmpfs) and the user is warned |
| `kp-sync session [-w] [USER@]HOST[:PATH]` | stage the current local hub DB and key file without a cloud pre-sync, create a per-session key, open ssh (without agent forwarding) with a reverse tunnel, and have the remote mount the staged directory with sshfs through a confined SFTP server (see "Remote hosts"); hold a lease for the session; normal daemon polling continues while it is open; its end runs the post-sync and removes the session key and its `authorized_keys` line |
| `kp-sync service start\|stop\|status ssh-session\|access-owner NAME` | manage a named SSH session or configured `kp-access-owner` process as a transient systemd user service; see Configuration |
| `kp-sync access-op` | `--file-op-cmd` for `kp-access` (see Integration) |
| `kp-sync sftp-serve ID` | forced command of a session key: `sftp-server` confined to `sessions/<id>/` (see "Remote hosts"); not for direct use |
| `kp-sync restore [--merge \| --replace] (--version SHA \| --backup NAME \| --file PATH) [--dry-run]` | bring back old content (see "Restoring an old version"); `--merge` is the default |
| `kp-sync history` | list the versions kept in the history (date, host, parents, entry count when the password is cached) and the backups of this boot |
| `kp-sync forget` | revoke the profile's cached passwords (main and per-copy) |
| `kp-sync keepassxc-disconnect` | remove this host/profile's local browser pairing without contacting KeePassXC; remote revocation is performed in KeePassXC |
| `kp-sync status [--json]` | cached endpoints/states, persistent copy history with how far behind it is, separately identified live/unfinished/historical-unknown sessions, leases and pending decisions; retired sessions are omitted; read directly from the run dir when the daemon is not running. The only local check: when this machine's hub no longer matches the version recorded for it, it is shown as "saved since the last sync" (unsynchronized changes, or a plain re-save), so offline edits are visible without contacting anything. It also says when each cloud was last reached from this machine (`cloud-contact.json` in the run dir, written after every run that observed it; lost at reboot, when the cloud copy's last synchronization from shared history is shown instead) |
| `kp-sync retire-session [--identity ID] [--confirm ID]` | preview legacy session identities read-only, or retire one identity after exact confirmation and runtime revalidation; shared publication outcomes are reported separately |
| `kp-sync service install daemon [--desktop]` | write or refresh selected-profile units and required shared templates; reload systemd without enabling or starting; desktop entries opt-in |
| `kp-sync doctor [--self-test]` | check the installation and report what to fix: units installed and enabled, socket reachable and protocol version, menu entry override still in effect and still calling `kp-sync run` (a KeePassXC update may change the system entry), KeePassXC's reload option, key file readable, rclone remote and probe working, hub on an encrypted disk when `require_encrypted`, a key written by a client readable by the daemon (keyring possession), swap encrypted or absent (`$XDG_RUNTIME_DIR` is tmpfs, which can be swapped out), that every `kp-sync` call in udevmon's actions and kp-access's `[owner] file-op-cmd` selects an existing profile (no `--profile` only with `config.toml`) and, for this profile, that kp-access's `db-path` is its staged copy, and for sessions `bwrap` (and unprivileged user namespaces), `sftp-server`, `~/.ssh/authorized_keys` editable (regular file, owned, not group/other-writable), no stale `kp-sync-session-*` lines, and the system sshd reachable on localhost. Explicit `--self-test` authorizes a five-minute restricted key and a confined localhost SFTP put/get round trip; removes only its own key and runtime artifacts afterwards, never uses the database/key file, and catches shell startup output that breaks SFTP |
| `kp-sync daemon [--foreground] [--debug]` | run the daemon in the terminal for debugging (the service must be stopped); it never detaches, so `--foreground` changes nothing |

### Status presentation (implemented)

Human-readable `status` uses one consolidated block per copy rather than
repeating classification and recorded-copy rows. Group persistent copies
separately from sessions. Lead with copy type, readable name and recorded
state; place version hashes and technical identities on separate secondary
lines. Human-readable status abbreviates hashes to at least 12 hexadecimal
characters, extending prefixes to distinguish versions in loaded history
and displayed status. `status --long` shows full hashes; JSON always retains
full hashes. Prefixes are display-only, not restore identifiers. This favors
narrow-terminal readability over compact wide tables.
Lead with a concise summary of recorded copies, unfinished sessions and
pending decisions, counting identities once. Historical/unknown lifecycle
does not count as established unfinished work.

Use configured endpoint names plus remote paths for cloud copies, host
names for hubs, optional drive aliases, and host/session identifiers for
SSH sessions. Missing names fall back to technical identities; do not
guess a friendly name from unavailable runtime records.

Distinguish cached synchronization observations from current lifecycle
evidence. Label versions as recorded, do not infer reachability from copy
history, and only claim attachment when runtime evidence supports it.
Show available last-synchronized/last-observed timestamps with readable
ages, labelled according to their actual meaning. Missing values remain
unknown; a history-record refresh is not proof of fresh endpoint contact.
Session blocks distinguish live, retained/unfinished and historical/unknown
lifecycle; retired identities are omitted as specified in Baseline store.
Show decisions and leases separately without implying they are additional
database copies.

The JSON interface retains existing machine-readable identities and fields.
Readable names and lifecycle annotations are additive; aliases must never
replace identity keys or modify synchronization semantics. Daemon-backed
and direct status must apply the same naming and presentation rules.
Presentation/aliases are separate from retirement behavior, and use the same
session lifecycle terminology in daemon-backed and direct status. Ages are
calculated from each copy's recorded synchronization time;
they are not endpoint-observation or reachability claims. JSON retains its
existing identity keys and fields, with display names, lifecycle, summary,
and timestamp/age values added.

`retire-session` without `--confirm` fetches available shared history for a
read-only preview; it may contact configured baseline stores, but it does
not persist or publish history. Applying requires `--identity` and a
byte-for-byte matching `--confirm`, then rechecks runtime evidence while
holding the access-operation and history locks. It does not remove database
copies. A successful local marker remains authoritative for later
publication if a shared store is unavailable.

Global options: `--config`, `--profile`, `--ui`, `--no-wait`,
`--no-daemon`, `--verbose` / `--debug` (message levels are defined under
Logging in the architecture chapter).

### Explicit maintenance commands

`cleanup` previews obsolete base files across attached configured baseline
stores and the runtime cache. `--store NAME` selects a configured endpoint;
`--runtime` selects only the local cache. Apply requires `--apply` and one
explicit scope. Shared apply additionally requires
`--confirm-writers-paused`: the operator must pause every other writer
sharing this history for the entire operation, including other profiles.
Preview reports candidate filenames/bytes, protected files and blockers;
apply reports deleted filenames and partial errors in the same JSON report.
The [baseline cleanup contract](history-and-sync.md#explicit-base-cleanup)
owns retention and concurrency rules.

`repair-history` previews retained versions whose base files are missing
from available history stores and whether each is recoverable; it prints
a JSON report and changes nothing. `--apply --confirm-writers-paused`
restores recoverable bases and removes versions whose base exists nowhere.
[Missing bases and repair](history-and-sync.md#missing-bases-and-repair)
owns the rules.

`service start|stop|restart|status daemon` manages the default daemon unit or
the instance selected by `--profile`. It accepts no resource name, unlike
the existing named SSH/access-owner commands. Custom configuration paths
without a matching installed profile unit are rejected. Operations run
directly through systemd, not through the daemon being managed. Start and
restart verify control-socket readiness; status never activates the daemon.
Stop does not disable socket/path/timer activation, which may start it again.
Failures are explicit; no forced-kill fallback is added. A stop counts as
clean when systemd reports a normal exit or SIGTERM, or, once nothing
references the stopped unit (its triggers already stopped, as in
`disable --now` and `uninstall`), no exit record with `Result=success`
and an inactive unit: an unclean stop leaves the unit failed, and systemd
keeps failed units with their record.

`service install daemon [--desktop]` writes or refreshes selected-profile
units and required shared templates, then reloads systemd without enabling
or starting anything. Menu/autostart entries are written only with
`--desktop`. Existing activation settings are not changed; file updates
can affect already-active units, and desktop entries affect future launches.
Units and entries name the program by the absolute path of the running
`kp-sync` (symlinks kept, so `~/.local/bin/kp-sync` stays retargetable),
because systemd resolves a bare `ExecStart` name only in fixed system
directories; files naming another `kp-sync` path are still recognized as
generated. Before writing anything, install checks every target
directory and that systemd can start that program, and reports all
problems together; `doctor` repeats the program check on the installed
daemon unit.
The old `install-units` CLI is removed rather than retained as an alias.
The Python `install_units` helper retains its existing default behavior;
the service command explicitly requests files-only and optional desktop.

`service enable daemon [--now]` requires installed units and enables the
configured socket, hub path and idle-sync timer without rewriting files.
`--now` also starts those triggers, not an unconditional daemon launch.
`service disable daemon [--now]` disables all three activation units,
including ones no longer selected in configuration. Without `--now` it
does not stop running units; with it, stop triggers first, then any idle-sync
client service, then gracefully stop/check the daemon.

`service uninstall daemon [--desktop]` disables/stops as above, removes
selected-profile generated unit files and reloads systemd. Shared templates
are conservatively retained, including when no other installed profile is
known, so other profiles and template instances are never broken.
Desktop menu/autostart entries remain unless `--desktop` is explicit.
Removal refuses unsafe, modified or other-profile files before management
begins and rechecks file contents before unlinking. Refused customization
requires manual review; uninstall does not remove drop-ins or vendor units.
Configuration, databases, runtime recovery data and named transient services
are never deleted. Partial failures are reported without claiming rollback.
Repeated disable/uninstall tolerates missing units/files.

These actions support only `daemon`, not transient SSH/access-owner units.
`service status daemon` reports daemon active state plus each trigger's
active and enabled state, without activation. Disabling is not masking or
application suspension: explicit requests, desktop/udev launchers and client
fallback startup may still start the daemon. Do not issue such requests
concurrently with disable/uninstall when a stopped daemon is required.

Exit codes: 0 consistent (or request accepted, with `--no-wait`), 1 error
(including a daemon that cannot be reached or started), 2 usage, 3
decision pending, 4 some endpoints were unreachable. When several apply,
the most important wins: 1, then 3, then 4. `status` uses 0 (in sync) and
10 (sync would change something).

Without the daemon, `status` reports the last sync's cached observations and
the runtime history, including unreachable recorded copies and pending
decisions; it does not contact endpoints. Use `sync --no-daemon --dry-run`
for fresh probes and classification without promotion or shared-state writes.
