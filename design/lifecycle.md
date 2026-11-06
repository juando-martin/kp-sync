# Daemon and runtime lifecycle

[Design index](../DESIGN.md)

## Daemon and leases

`kp-sync` runs only while something needs the database. One daemon per
user and profile (`kp-sync daemon`, the systemd user service
`kp-sync.service`) does all syncing on that machine; every other `kp-sync`
command is a short client that sends it a request over a Unix socket.

- **Activation**: `kp-sync.socket` listens on
  `$XDG_RUNTIME_DIR/kp-sync/control.sock` (socket 0600, directory 0700) and
  starts the service on the first request. The daemon checks the peer's
  uid (`SO_PEERCRED`) and rejects anyone else. Requests and replies are
  JSON lines with a protocol version and a small fixed set of requests
  (`sync`, `attach`, `detach`, `run`, `up`, `down`, `eject`, `lease`,
  `release`, `status`, `resolve`, `restore`, `access-op`, `ui-reply`).
  Each message is bounded to 1 MiB; unexpected fields, non-finite
  JSON numbers and invalid argument shapes are rejected. A client of
  another version is refused with a
  clear error. No secret ever crosses the socket (a password typed in a
  client's terminal goes straight into the keyring, see Credentials).
- **Thin daemon**: the daemon only queues requests, tracks leases and
  triggers, and owns the UI; every decision about copies is made by the
  sync engine, the same code `--no-daemon` runs. Time, systemd and D-Bus
  are reached through small interfaces, so the daemon is tested with a
  fake clock and fakes of both. `kp-sync daemon --debug`
  runs it in a terminal for debugging (after stopping the service).
  Without systemd, a client that finds no socket starts the daemon itself
  with `setsid`. `--no-daemon` runs the engine in-process (tests,
  debugging), guarded by the `lock` flock so it never runs beside a daemon
  sync. With several profiles on one machine, each has its own instance
  (`kp-sync@PROFILE.service`), socket and run directory: the default
  profile uses `$XDG_RUNTIME_DIR/kp-sync/` and the runtime hub
  `$XDG_RUNTIME_DIR/pass.kdbx`; any other profile uses
  `$XDG_RUNTIME_DIR/kp-sync-PROFILE/` and `$XDG_RUNTIME_DIR/PROFILE/pass.kdbx`
  unless it sets `hub`. Every path below is relative to the profile's run
  directory.
- **Leases**: the daemon keeps working while at least one lease is held.
  A lease is held by:
  - an application started through the wrapper (`kp-sync run`), for the
    life of its systemd unit (see "Applications");
  - an application started without the wrapper, found through D-Bus or
    by process name (see "Applications");
  - an attached pendrive, from `attach` to `detach`, so the drive is kept
    current and can be removed at any moment;
  - an sshfs session, for the life of the `kp-sync session` client;
  - a kp-access client, from `BEGIN` to `END`, renewed by every operation
    and expiring after `access_lease_ttl` (default 1 h) without
    operations; expiry only stops it from keeping the daemon running
    (`staging/` stays until `END`, and the next operation takes the lease
    again);
  - pending decisions, while there is a display to ask on.
  Connection-bound leases (sshfs sessions, waiting clients) end when the
  client's connection closes, so a crashed client cannot pin the daemon.
  Named leases (drives, kp-access) are kept in `leases.json` and survive
  a daemon restart. Application leases need no client: on start the
  daemon looks for active `kp-sync-app-*` units and for the KeePassXC
  D-Bus name and takes their leases again, so `kp-sync run` returns once
  the application is started.
- **Named access services**: `kp-sync service start|stop|status` manages
  `[ssh_sessions.NAME]` and `[access_owners.NAME]` entries as transient
  systemd user services. A named SSH service runs the normal session owner
  without an attached terminal; it retains the SSH connection and lease
  until stopped, then revokes the key and runs the normal post-sync. A named
  owner runs its configured argv directly in the foreground, forwarding
  stop signals and suppressing child stdout/stderr; lifecycle messages and
  errors go to the journal. These units survive terminal closure, but are
  not enabled to survive logout or reboot. They do not replace or modify the
  daemon's installed units.
- **Daemon service controls**: `kp-sync service start|stop|restart|status
  daemon` addresses the installed default/profile unit directly through
  systemd. It does not send its lifecycle request through daemon IPC.
  Start/restart verify socket readiness, and status does not activate it.
  Stop preserves the existing graceful final-sync/recovery behavior and
  leaves socket/path/timer activation enabled. These commands do not install
  units or authorize production operations during implementation testing.
  Files-only install, activation enable/disable and profile-scoped uninstall
  are separate
  actions, with systemctl-like `--now` semantics. Their file-preservation,
  desktop scope and reactivation limits are owned by the
  [command contract](configuration-cli.md#explicit-maintenance-commands).
- **Life cycle**:
  1. First application or kp-access lease: sync (the **pre-sync**). A
     remote session instead stages the current local hub and takes its lease
     without a pre-sync, so startup does not wait on the cloud. Staging holds
     the engine flock while copying the hub and recording the session's
     exact snapshot hash if it is already in the local version DAG. The first
     remote save then has known ancestry even before the first poll.
     An uncommitted/unknown snapshot is not invented as a committed version;
     it retains the normal unknown-copy review safeguards. A wrapper
     waits for pre-sync up to
     `prestart_timeout` (default 20 s) when a hub is available; if the cloud is slow or
     unreachable, the application starts on the hub as it is. When the
     pre-sync finished without reaching the cloud, a warning dialog
     (zenity/kdialog; a notification with a custom command or without a
     display) says so and how long it has been since the cloud was last
     reached, shown beside the application without delaying it; with
     `confirm_without_cloud` the same text is a question asked before
     launching (no launch on "no"). A pre-sync still running at the
     timeout gives no warning when the application starts. *Since 0.5.0:* when that pre-sync later finishes
     without reaching the cloud, the same warning dialog is shown then,
     beside the running application, in both modes (it can no longer be
     a question; the application is open). The cloud reach check
     (endpoints chapter) makes a clearly offline pre-sync finish within
     `prestart_timeout`, so the warning, or the `confirm_without_cloud`
     question, normally comes before launch. While
     the cloud stays unreachable, each daemon sync (including the hourly
     idle sync) notifies, at most once per `cloud_warning_hours`, when
     the hub holds changes not yet synchronized and the cloud has not
     been reached for that long. If the hub is absent
     at that timeout, the launch request waits for the same pre-sync to
     finish, then launches only if the hub exists. It does not abandon the
     launch merely because downloading the first copy takes longer than
     the fast-start timeout. If no copy could be
     reached and the hub does not exist, the application is not started
     on it: the user is told that no copy of the database is available
     (insert a pendrive or check the cloud), so KeePassXC is never left to
     create an empty database at the hub's path.
     Configured writable pendrives retain history for cold offline startup
     under the [baseline store contract](history-and-sync.md#baseline-store).
     A drive lacking history is not automatically trusted to bypass pending
     unknown-copy decisions, even when it is the only reachable database.
  2. While leases are held: watch the hub's directory and sync after each
     save, sync when a drive is attached or a session uploads, and poll
     the endpoints every `poll_interval` to bring in cloud edits (phones).
  3. A lease ends: sync (the **post-sync**), so whatever that client
     changed is promoted at once.
  4. No lease left: after `idle_exit` (default 2 min, which avoids a
     restart for a quick relaunch) the daemon exits. While it is not
     running nothing is synced, apart from the triggers below.
- **Triggers without a lease**: any request (`sync`, `attach`, `status`,
  ...) starts the daemon through the socket. With `hub_path_unit`
  (default true) a systemd path unit (`kp-sync-hub.path`,
  `PathChanged=` on the hub file itself, not its directory, which in
  `$XDG_RUNTIME_DIR` or `~/data/` changes all the time; systemd also
  watches the parent for renames, so rename-saves are caught) starts it
  when the hub is saved by a program it did not know
  about; that save is synced, as a foreign change if a sync had rewritten
  the hub while that program might have had it open. With `idle_sync`, a
  systemd timer (`kp-sync-sync.timer`, at login and every
  `idle_sync_interval`, default 1 h) asks for one sync without holding a
  lease. It is on by default for a persistent hub (so the second laptop
  has the latest content when it later goes offline) and off for a hub in
  `$XDG_RUNTIME_DIR`, which usually does not exist when nothing uses it.
- **Serialisation**: the daemon runs one sync at a time. Triggers that
  arrive during a sync are coalesced into one more run afterwards, so
  nothing is lost and no caller is told "busy". Waiting for the user never
  blocks other syncs (see "User interaction"). A background request
  (`ui = none`) that is joined by a client with a UI takes that UI, so the
  waiting client's questions are asked rather than deferred. `down`
  keeps the hub while any sync is in flight, including one paused at a
  question with the sync slot released.
  Hub and session file-change detection is deferred while the sync slot
  is held: a completed run records the committed hash, not an arbitrary
  end-of-run file snapshot, so its own writes do not trigger another run
  but a later save still does. Explicit and drive triggers remain queued.
  When a question releases the slot, file-change detection remains active.
  The session client owns its normal post-sync; dropping its connection
  lease or removing an already-cleaned session record does not request
  another sync. Crashed owners are handled by the dead-session worker,
  which revokes access, post-syncs and retains the staged copy on failure.
  After successful cleanup, private retirement progress is written before
  staged files are removed. The daemon retries cleanup after a crash, then
  commits the session tombstone and clears progress only after verifying it
  in runtime history. Pending or failed sync/cleanup leaves the staged data
  and recovery record intact. `kp-access END` uses the same ordering while
  holding `access-op.lock`; access lifecycles have unique history identities
  so cleanup never tombstones a reusable identity.
- **Stop**: on `systemctl --user stop`, logout or shutdown the daemon runs
  a final sync with `--ui none` within the unit's `TimeoutStopSec`
  (default 90 s). While a lease is held it takes a logind *delay*
  inhibitor for shutdown and sleep, released after the final sync (or
  after logind's `InhibitDelayMaxSec`), so shutdown and suspend wait for
  it. Logout cannot be delayed this way.
- **Unsynced hub (rescue)**: where the hub lives in `$XDG_RUNTIME_DIR`,
  logout wipes it. As soon as a save cannot be promoted to any other copy,
  the daemon notifies "pass.kdbx is not synced anywhere else: insert a
  pendrive to save it" (with a *Save to pendrive* action), and `status`
  shows it. Any KEYDRIVE drive attached then is synced normally. Another
  writable removable drive mounted while the warning is active receives a
  rescue file `pass.rescue-HOST-TIME.kdbx` (encrypted like any copy; the
  key file is never written next to it). At stop, if the hub is still the
  only copy of its content and a display is available, the same offer is
  shown as a dialog that waits up to `rescue_wait` (default 60 s, within
  `TimeoutStopSec`) for a drive. A rescue file is brought back with
  `kp-sync restore --merge --file`, or by attaching the drive. Nothing is
  written to the machine's own disk.
- **Crash**: the service has `Restart=on-failure`; named leases and
  pending decisions are reloaded from the run dir. Clients holding a
  connection-bound lease (a `kp-sync session` client, a waiting client)
  reconnect through the socket and take their lease again; until they
  do, a session whose owner process is alive counts as a lease.
- **Periodic tick failures**: each refresh step is isolated and logged.
  Transient adapter failures (drive discovery, application/session refresh,
  hub watch or inhibitor update) back off exponentially up to
  `poll_interval` (60 s when polling is disabled) and preserve the last known
  leases; after five consecutive failures of one step, the daemon exits so
  systemd can restart it. A malformed
  `pending.json` is different: keep the daemon serving status and other
  requests, hold a pending lease, notify the user, and refuse sync requests
  until the file is repaired. Never overwrite or discard corrupt pending
  state; `doctor` reports it.

## Runtime state (all under `$XDG_RUNTIME_DIR/kp-sync/`)

For a non-default profile, read the profile's run directory instead (see
"Daemon and leases").

```
bases/         <sha256>.kdbx, one file per version kept in the history
state.json     schema version; versions (sha, parents, generation, host,
               time, content hash, size); copies (key, version held, file
               hashes as seen there: SHA-256 and whatever the remote offers,
               e.g. MD5; record generation; last sync time and host);
               terminal retirement markers keyed by exact session identity
drives/        <identity>.json per attached pendrive (mount point, identity,
               attach time)
backups/       timestamped copies taken before any overwrite (pruned to N)
sessions/      <id>.json per active session (host, mode, pids of the owning
               client and of its ssh), <id>.key (its ssh key pair) and
               <id>/, the directory exported to that session, holding only
               the database and the key file (its public key is a line in
               ~/.ssh/authorized_keys, see "Remote hosts")
session-retirements/
               private cleanup progress, removed only after runtime history
               retains the corresponding terminal marker
diagnostics/   isolated doctor SFTP exports and short-lived session keys;
               never discovered as database endpoints or daemon leases
rclone/        `<endpoint>.conf`: private copy of the externally provided
               rclone config, removed under the draining rules in
               "Integration points" (approved, not yet implemented); the
               external file itself is never touched
staging/       kp-access ops dir (access-op); holds only the database, its
               temporary upload names and nothing else
tmp/           fetched copies during a run (removed afterwards)
control.sock   the daemon's socket (0600)
leases.json    named leases (drives, kp-access) for a daemon restart
lock           flock held during a sync; keeps `--no-daemon` runs and the
               daemon apart
pending.json   decisions waiting for the user (see "User interaction")
choices.json   conflict choices already made, reused if a merge is redone
inflight.json  locally staged candidate versions awaiting consistent promotion
log            current-boot log (also sent to the journal / stderr)
```

Directories are 0700, files 0600. All writes of `state.json` and copies are
atomic (write to a temporary file in the same directory, fsync, rename).

`bases/` and `state.json` here are a working cache of the baseline store
(below). Everything else, including backups, is lost on reboot (also on a
machine with a persistent hub: only the hub itself is persistent there). The durable
backups are Google Drive's version history and the base files still kept in
the baseline store (each is a complete earlier version). `kp-sync` never
deletes or trashes remote versions of the database.
