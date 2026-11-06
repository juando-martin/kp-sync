# Endpoints and remote access

[Design index](../DESIGN.md)

## Machines and pendrives

- **Every machine is a hub for itself.** The main laptop and, for example,
  a second laptop each run `kp-sync` with their own profile. They never
  talk to each other directly: they meet through the cloud copy, the
  pendrives and the shared history in the baseline store. Whichever machine
  sees a divergence merges it.
- **Persistent hub** (second laptop): KeePassXC opens the hub file
  directly, launched through the wrapper (menu entry, autostart), and the
  path unit catches saves from launches that bypass it. `down` and
  `detach` never delete or move the hub. The rest of the run dir stays in
  `$XDG_RUNTIME_DIR`.
  Deferred second-laptop acceptance uses a private source/configuration
  bundle (Git bundles of kp-sync, unchanged udevmon and its local keyctl
  dependency, cloned locally with history for later updates).
  It contains no credentials or database files, requires local path/drive
  identity review, and does not install or enable integration automatically.
- **Pendrives on any machine**: udevmon sends `kp-sync attach` when a drive
  is mounted and `kp-sync detach` when it goes. `attach` takes a lease and
  syncs the hub, the cloud and that drive; on a machine with a persistent
  hub it does nothing else. On the main laptop `up` is `attach` plus
  starting KeePassXC through `run`.
- **Outdated pendrives**: a drive holding an older known version is
  `stale` and is simply updated (backup first, no password). A drive
  edited somewhere is `changed` and merged against its own last version.
  A drive whose bytes are unknown is `new` and goes through the
  "Unknown copies" check, so an old stick never wins only because it
  differs, and never brings back deleted entries. With `notify_stale`, the
  user is told "KEYDRIVE (Samsung 2) was 3 versions behind, updated".
- **Several drives at once**: each is an endpoint instance. Rclone
  credentials are provisioned separately from attachment; see "Rclone
  credentials and `drive-sync`" under Integration points.
- `status` lists retained persistent copy records (hubs of all machines,
  cloud, every drive), their recorded versions, how many versions behind
  they are and when they were last synced. These are historical
  observations, not proof of present reachability. Disposable sessions
  are listed separately as live or retained for unfinished cleanup/sync;
  retired sessions are not shown as outdated copies. Legacy historical
  session records whose lifecycle cannot be established are labelled as
  historical/unknown, not as live endpoints or work awaiting an update.

## Integration points

- **udevmon drive actions** (`kp-sync drive-sync`; the old `keydisk.py`
  hook and `keydisk.sh` are retired)
  - udevmon passes the event to `kp-sync drive-sync` (see "Rclone
    credentials and `drive-sync`" below), which validates the mount and
    writes a private pending-up marker keyed by profile and physical
    identity in the shared runtime directory. The daemon removes that
    marker only after registering the drive under its lifecycle gate. An
    older down request must retain the hub while any such marker exists,
    including when a drive was reinserted during its slow cloud post-sync.
    Failed submission retains the marker safely; retry up to complete
    registration.
  - kp-sync's actions live in its own udevmon drop-in,
    `~/.config/udevmon/config.d/50-kp-sync.toml` (udevmon 0.7.0+), with
    the default `order` (50) and `notify_on_failure = true`; the rclone
    provider's drop-in wraps them with `order` 10 (`up`) and 90 (`down`).
    Their order depends only on `order`, not on file names. A drive set
    up while plugged in is attached with `udevmon trigger NAME`.
  - Actions are clients only: they send a request to the daemon and return
    at once (`--no-wait`), so the action's `timeout = 120` and udevmon
    restarts no longer matter. The daemon and the applications run in
    their own user units.
  - Both actions pass the drive identity: on mount events udevmon gives
    `{drive_serial}` and `{fs_uuid}` (`$UDEVMON_DRIVE_SERIAL`,
    `$UDEVMON_FS_UUID`; `serial` exists only on USB device events), so
    `drive-sync` passes `--drive-id "$drive_serial:$fs_uuid"`.
    The engine independently identifies mounts from `lsblk --json --tree`,
    inheriting the physical disk serial through its partition children
    and combining it with the mounted filesystem's UUID.
  - `up` (startup/mounted): on the main laptop `kp-sync up --drive-id ...
    "$mount_point"`; on other machines `kp-sync attach --drive-id ...
    "$mount_point"`; rclone credentials are the provider's concern.
  - `down` (unmounted): runs after the drive is gone, so it cannot write to
    it (`$UDEVMON_SURPRISE` tells a clean unmount from a pulled stick, for
    the warning). On the main laptop: `kp-sync down --drive-id ...`, which
    quits KeePassXC, runs the post-sync and only then removes the hub.
    The daemon removes only its private rclone copies; the provider's
    `down` must follow this request. On other machines: `kp-sync detach
    --drive-id ...`. With several drives attached, `down` for one of them
    only detaches it.
  - The pendrive is kept current by every sync while mounted (after each
    save and every `poll_interval`, since an attached drive holds a
    lease), so the desktop's own eject button, which bypasses `kp-sync`,
    normally loses nothing. `kp-sync eject` adds a final sync for the
    remaining window. Pulling it out with a sync still due is an unclean
    removal: `kp-sync` warns and the next `attach` of that drive, on any
    machine, reconciles it through the history.
- **Rclone credentials and `drive-sync` (implemented)**
  - *Separation.* Drive attachment and rclone credential provisioning are
    independent udevmon actions. A KeePass-only drive needs no rclone
    config; an rclone-only drive does not involve kp-sync. Missing
    credentials never prevent attachment or offline use.
  - *Provider (outside this repo).* A udevmon-side helper publishes the
    config at `$XDG_RUNTIME_DIR/rclone/rclone.conf` (atomic, 0600) while a
    tagged source drive is present, and deletes it when the last source
    leaves. With several drives the first published config stays while its
    drive is present; differences are only logged. The provider owns that
    path; kp-sync never writes, renames or deletes it. The provider
    specification links here and does not restate this contract.
  - *Private copy.* The endpoint's `rclone_config` names the external
    file. kp-sync passes rclone its own copy,
    `<profile run dir>/rclone/<endpoint>.conf` (0700/0600), so each profile
    has independent credentials and no cross-profile deletion check
    remains. An endpoint without `rclone_config` keeps using rclone's own
    default and is unaffected.
  - *Import.* Under the sync slot and only while no rclone child of the
    profile runs, a changed external file (by hash) is copied in
    atomically at the start of a run. A change seen during a run waits
    until it ends, so a running rclone cannot write an old refreshed
    token over a newer copy. A config change is treated as the same cloud
    endpoint: kp-sync does not detect account changes, so all drives
    tagged for rclone must carry the same account. Rotating-refresh-token
    backends are unsupported; Google Drive does not rotate.
  - *Unavailable.* With no external file and no copy, the endpoint is
    reported as "credentials unavailable": no rclone call, skipped and not
    queued, with distinct `status`/log wording. An invalid or revoked
    config remains an ordinary rclone failure.
  - *Early removal (draining).* When the external file disappears, the
    copy allows exactly one more synchronization (the run in flight or one
    queued, coalesced with a `down` post-sync) and is deleted when it
    ends or releases the sync slot for a user question, never while an
    rclone child runs. A resumed run finds the cloud unavailable and
    leaves its decision pending. Reappearance before deletion cancels
    draining. A leftover copy found at daemon start with no external file,
    and every copy at runtime cleanup or idle exit, is deleted. kp-sync
    never writes refreshed tokens back to the source drive.
  - *Cross-process lock.* "No rclone child runs" must hold across all
    processes of the profile, not only inside one sync: `history`,
    `doctor` and a daemon can use the same copy at once. Every rclone
    invocation with the private copy holds
    `<profile run dir>/rclone-credentials.lock` shared (concurrent rclone
    runs stay parallel); import, drain and discard hold it exclusive, so
    they wait for running rclone children and none starts meanwhile. A
    missing copy is imported first, then the shared lock is taken and
    the copy re-checked: if a drain removed it in between, the endpoint
    is "credentials unavailable". Waits are bounded: a busy import keeps
    the current copy (the next run imports), a busy drain is retried
    later (the daemon watch only tries without waiting), and a copy left
    at cleanup is removed by the next daemon start or by logout.
    An import or drain takes the lock only when it has something to do
    (a changed external file, or a copy without one); an unchanged run
    neither waits for nor blocks rclone children.
  - *Watch.* While running, the daemon watches the external file's
    directory (or its parent while absent, or polls): appearance or change
    imports and, while leases are held, starts a sync; disappearance
    starts draining. There is no provider-to-consumer call.
  - *Action ordering.* Per event, `rclone-config up` precedes the kp-sync
    `up`; the kp-sync `down` (accepted, though `--no-wait`) must precede
    `rclone-config down`, so the provider's `down` action belongs below
    the kp-sync actions even though its `up` belongs above them.
    Otherwise the draining run could end before
    the post-sync request arrives and the cloud copy would not be updated.
    The provider also removes the file on its `shutdown` event while the
    drive is still attached; kp-sync then drains and runs without the
    cloud until the next `startup` republishes it.
  - *Drive command.* `kp-sync --profile P drive-sync up|down --mode
    main|secondary --drive-id SERIAL:FS_UUID [MOUNTPOINT]` (mount point
    required for `up`) replaces the
    drive half of the external hook: it validates the drive identity
    against the mount, writes the pending-up marker under `keydisk.lock`,
    submits `up`/`down` (`main`) or `attach`/`detach` (`secondary`) with
    `--no-wait`, and keeps the marker if submission fails. `--profile`
    and `--mode` are required; no environment variable or default selects
    them. It is idempotent and never touches rclone files. The marker and
    `keydisk.lock` still protect the hub during a reinsert in the middle
    of a slow `down`. `~/.dynamic_config` is neither created nor read.
- **Optional rcd transport** (`rclone_transport = "auto"` on an `rclone`
  endpoint, the default since 0.3.3 after the owner tested it with the
  real daemon on `alpha`; `"cli"` keeps plain rclone commands). Each rclone call re-authenticates and
  re-resolves its paths (1–4 s on Google Drive); a persistent
  `rclone rcd` keeps both in memory (`src/kp_sync/rclone_rcd.py`).
  - *Socket only.* `rcd` listens on
    `<profile run dir>/rclone-rcd/<endpoint>.sock`, in a 0700
    directory (the socket itself is created 0775), and runs with
    `--rc-no-auth`, which is acceptable only there: without it rclone
    refuses `operations/*` with HTTP 403 even on a Unix socket. There is
    no loopback TCP fallback.
  - *Selection.* Unix sockets need rclone 1.61; it is detected by
    starting `rcd` and calling `rc/noop` within 10 s, not by parsing
    versions (1.60.1 exits at once with `listen tcp: … unknown port`).
    A failed start logs once and the process uses commands for the rest
    of its life. Only the exact argument vectors kp-sync generates are
    translated (`lsjson --hash PATH` to `operations/stat`, or
    `operations/list` for a directory; the filtered recursive listing to
    one `operations/list` with the same include rules and depth;
    `copyto` to `operations/copyfile`, which updates Drive files in
    place like `copyto`; `deletefile`/`rmdir`); custom
    `download`/`upload`/`probe` commands and the batched base transfers
    always run as commands. Listings are identical to `lsjson` output,
    so probe tokens do not change with the transport.
  - *Ownership.* Only the daemon starts and stops `rcd` (lazily, one per
    endpoint, kept across runs, stopped at idle exit and shutdown). Other
    processes (`--no-daemon`, `history`, `doctor`, …) use its socket
    while it answers `rc/noop` and plain rclone otherwise; they never
    start one. A leftover answering socket from an earlier daemon is told
    to `core/quit` before a new `rcd` starts; stdout/stderr are
    discarded.
  - *Credentials (F8).* `rcd` reads its `--config` once, keeps the
    credentials in memory and writes refreshed tokens back, with no
    reload call. With a private copy it therefore holds
    `rclone-credentials.lock` shared for its whole lifetime: the daemon
    takes the lock and the `rcd` child inherits the locked descriptor,
    so the lock ends exactly when `rcd` exits, even if the daemon dies.
    An import or drain (run start/end, the daemon's watch, runtime
    cleanup and shutdown) stops the daemon's `rcd` first; it restarts on
    the next call if credentials remain. A missing copy starts no `rcd`.
  - *Failures.* Decided conservatively in the unattended run of
    2026-11-04. Every operation runs as an `_async` job polled with
    `job/status` and limited by the endpoint's `timeout`. A timed-out
    command is killed, but a job keeps running in the server, so on
    timeout the job is asked to stop (`job/stop`) and awaited for 5 s;
    if it is still running the daemon stops its `rcd` (another process
    sends `core/quit`), so no write can land after the run has moved on
    to re-check the target. The call then fails like a timed-out
    command. A failure after a request was sent raises exactly the
    exceptions of a failed rclone command (`object not found` and
    `directory not found` count as missing, like exit statuses 3/4; an
    unknown method's 404 does not), so the existing retry and restart
    rules apply and the operation is never repeated as a command. Only
    when the socket could not be reached, so nothing was requested, does
    the call run as a command instead.
- **Cloud reach check** (since 0.5.0:
  `reach.py`, `RcloneEndpoint._ensure_reachable`; PLAN "Proposed
  improvements" 11). Offline, rclone retries until the
  endpoint `timeout` (30 s), longer than `prestart_timeout` (20 s), so
  KeePassXC starts before the pre-sync knows the cloud is unreachable
  and the launch warning never appears (owner report of 2026-11-05). A
  check of a few seconds before a run's first rclone call settles the
  clearly offline case early.
  - *Only a shortcut to "unreachable".* A failed check skips the
    endpoint for this run exactly as an unreachable probe does
    (`EndpointUnavailable`, skipped and not queued, the same launch
    warning and notifications). A passing check decides nothing: the
    filtered listing and every later call run as today, and their
    failures count as before. Availability is still the rclone probe
    itself (configuration chapter), never a ping.
  - *Steps,* within `reach_timeout` (default 3 s) in all. 1. Ask
    NetworkManager for its `Connectivity` property (`busctl --system
    get-property`, at most 1 s), without calling `CheckConnectivity`:
    only `portal` fails ("captive portal"), since a TCP connection can
    succeed through a portal. `none` is not trusted: NetworkManager
    reports it also when it does not manage a working connection
    (manual interface, tether), and a truly offline connection fails
    at once in step 2 anyway. 2. Open a TCP connection to the target:
    resolve it (in a helper thread, since `getaddrinfo` cannot be timed
    out) and connect to up to six addresses at once, so a dead IPv6
    route cannot use up the time IPv4 needs; any accepting address
    passes. Nothing is sent, and the sockets are closed at once.
  - *Target.* `reach_check` on the endpoint: `"auto"` (default),
    `false` (no check) or `"HOST:PORT"`. `auto` takes the remote's type
    and only non-secret keys (`type`, `remote`, `provider`, `region`,
    `endpoint`, `url`, `host`, `port`) from the rclone config the
    endpoint already uses (its private copy or `rclone_config`, else
    rclone's own lookup: `RCLONE_CONFIG`, `$XDG_CONFIG_HOME` or
    `~/.config` `rclone/rclone.conf`, `~/.rclone.conf`), with
    `RCLONE_CONFIG_NAME_KEY` variables overriding it as in rclone;
    tokens are never read into the result or logged, and an encrypted
    config gives no target. Known types: `drive` → `www.googleapis.com:443`,
    `onedrive` → `graph.microsoft.com:443`, `dropbox` →
    `api.dropboxapi.com:443`, `box` → `api.box.com:443`; `s3`, `webdav`,
    `sftp` and `ftp` take their host and port from `endpoint`, `url` or
    `host`/`port` (AWS `s3` without `endpoint`:
    `s3.REGION.amazonaws.com:443`); wrappers (`crypt`, `alias`,
    `chunker`, `compress`, `hasher`) follow `remote`, at most four
    levels. A remote with several upstreams (`union`, `combine`), an
    unknown type or an unreadable config has no target: step 2 is
    skipped (logged once per daemon) and the run behaves as today.
  - *Proxies.* rclone, like any Go program, honours only
    `HTTPS_PROXY`/`HTTP_PROXY` (either case) and `NO_PROXY`, and only
    for HTTP APIs (not `ALL_PROXY`, not SFTP/FTP). When the environment
    rclone runs with sets the matching variable and `NO_PROXY` (`*`, a
    host or a domain suffix) does not exclude the target, step 2
    connects to the proxy's host and port instead. An explicit
    `reach_check` is used as given.
  - *Scope.* Once per run and endpoint, before its first call. The
    verdict holds for that run only (a restart in the run reuses it);
    the next run checks again. Online, it costs one TCP handshake.
  - *Wrong verdicts.* A blocked or filtered check while rclone works
    would hide the cloud on every run, so `doctor` runs the check and
    the real probe and reports "reach check fails but rclone reaches
    the cloud: set `reach_check`". Each failure is logged with its
    reason ("cloud: not reachable (reach check: connect to
    www.googleapis.com:443 timed out after 3 s); skipped").
  - *After a timeout* (since 0.5.0). The first
    rclone call of a run that times out (command or rcd request) makes
    the endpoint unavailable for the rest of that run: later calls,
    including pre- and post-write re-checks, fail at once without
    running rclone, so a cloud that hangs costs one `timeout` per run
    instead of one per call (the history load and the database listing
    each waited 30 s before). Other failures (an exit status, a missing
    object, an rcd error) stay per call. `begin_run` clears it, and so
    does `end_run`, which also runs when a run pauses for a question,
    so a resumed run tries again. Skipping a re-check after a timed-out
    write is safe: the copy stays unverified for this run, and the next
    run classifies whatever landed by its content.
- **`~/bin/keepass`**: thin wrapper over `kp-sync run --app keepassxc`
  (`--legacy` selects `keepass-legacy`), keeping the name existing
  launchers use. The desktop menu entry and autostart call `kp-sync run`
  too (see "Applications").
- **kp-access (Windows)**: unchanged. Run `kp-access-owner` with
  `--file-op-cmd "kp-sync access-op"` and `--db-path` pointing at
  `staging/pass.kdbx`. A `--path-name` with a directory component (e.g.
  `/vault/pass.kdbx`) is preferred to limit server-side file operations;
  legacy root-level paths (e.g. `/pass.kdbx`) remain supported because
  `kp-sync` independently validates every operation against the configured
  database name and its temporary siblings. If `BEGIN` fails (daemon unreachable), kp-access
  falls back to reading `--db-path` directly, which then fails safely
  instead of serving the hub or an old file. `kp-sync access-op` replaces the
  `kp-access-file-op` mode of `keepassdb_ops.sh`; each call is a short
  client of the daemon:
  - `BEGIN`: take the kp-access lease, which makes the daemon create
    `staging/` and copy the hub there; `END`: release it (post-sync, then
    `staging/` is removed). `BEGIN` and `END` bracket the whole
    `kp-access-owner` run, which may last days; when the lease expires
    for lack of requests (`access_lease_ttl`), `staging/` stays and the
    next operation takes the lease again (with a sync first).
    `END` reacquires an expired lease before post-sync so staged changes
    are included. Unfinished uploads/transit retain staging and report an
    error. Upload creation and final post-sync/cleanup share `access-op.lock`;
    the client releases it before sending its daemon request.
  - `GET`: serve the staged file. Before that, if the staged copy is
    `stale` (unchanged since its last sync but behind the hub), ask the
    daemon to refresh it, waiting at most a few seconds; past that, serve
    it as it is. A `changed` staged copy (an upload not yet synced) is
    never overwritten.
  - `PUT`, `DELETE`, `MOVE`: re-validate the path (it must stay inside
    `staging/`, since `kp-access` does not approve these operations through
    its dialog; only an authenticated user and this command gate them;
    the daemon validates it again), apply the operation, and when the
    result is the database, check its KDBX signature and send a sync
    request without waiting. It must return quickly; a merge it causes is
    asked on the desktop or left pending. KeePass saves to a URL as
    `PUT pass.kdbx.tmp`, `DELETE pass.kdbx`, `MOVE pass.kdbx.tmp ->
    pass.kdbx`: while the staged database is absent after such a
    `DELETE`, the staged copy is *in transit* (neither `missing` nor a
    target, and `GET` is refused), and the sync is requested only after
    the `MOVE`. A transit not completed within a minute is reported and
    the copy is then treated as `missing` (its record unchanged).
  - Log to stderr with `INFO:` / `ERROR:` prefixes, which `kp-access-owner`
    re-logs.
  The staged copy is a `session` endpoint registered by `BEGIN`.
- **Remote hosts (sshfs)**: need only `sshfs` on the remote side, as
  today. Unlike `kp-ssh-access.sh` (`ssh -A` and a full login back to the
  laptop, which lets the remote reach the whole home directory and use
  the forwarded agent), the remote is confined to the staged copy:
  - `kp-sync session` creates a per-session ed25519 key as
    `sessions/<id>.key` (outside the exported directory, never on disk), sends the private key over the ssh
    connection to a 0600 file in the remote's `$XDG_RUNTIME_DIR` (or
    `/dev/shm`), used by sshfs with `IdentityFile` and removed at exit,
    and opens ssh without `-A`;
  - the remote reaches the laptop's system sshd through the reverse
    tunnel, as today; `sshd_config` is not changed. The remote gets the
    laptop's ed25519 host public key as its `UserKnownHostsFile` entry
    for the tunnel port (so sshfs runs with `StrictHostKeyChecking=yes`).
    (A per-session sshd run as the user was prototyped and works, but was
    rejected as harder to maintain: its own config, host key and port per
    session, and re-validation on every OpenSSH upgrade.)
  - the session key is added to `~/.ssh/authorized_keys` as one line,
    `restrict,from="127.0.0.1,::1",expiry-time="YYYYMMDDHHMM",command="/ABS/PATH/kp-sync --profile PROFILE sftp-serve ID" ssh-ed25519 … kp-sync-session-PROFILE-ID`.
    The command uses the absolute path of the installed `kp-sync`
    (`~/.venvs/tools/bin/kp-sync`, expanded), since sshd runs it with the
    system `PATH`, and names the profile.
    `restrict` disables forwarding, agent, X11, pty and `~/.ssh/rc`
    whatever `sshd_config` allows; `from=` admits only connections
    arriving through the tunnel; `expiry-time` (default `session_key_ttl`
    = 24 h, rewritten an hour before expiry while the session lasts)
    makes a leftover line useless even if nothing removes it. The line
    holds only a public key; this is the one sanctioned write to
    permanent storage outside the exceptions in "Goals".
  - `authorized_keys` is edited copy-edit-move, never in place: take an
    flock on `$XDG_RUNTIME_DIR/kp-sync-authorized_keys.lock` (shared by
    all profiles); refuse if the file is a symlink, not a regular file,
    not owned by the user or group/other-writable; read it, keep every
    line that is not a `kp-sync-session-*` line byte for byte, add or
    drop only our lines; write `~/.ssh/.authorized_keys.kp-sync.tmp`
    (0600) and fsync it; check that the result differs from the original
    only in our lines; re-stat the original (inode, size, mtime) and
    start again if it changed meanwhile (another editor); then
    `os.replace` and fsync `~/.ssh`. A missing file is created 0600 (and
    `~/.ssh` 0700).
  - lines are removed at session end. A session that ends without its
    client finishing (client crash, killed terminal) ends its lease when
    the connection closes (unless the daemon itself restarted, see
    "Daemon and leases"): the daemon stops the session's ssh process if
    it survived (pid in `sessions/<id>.json`, so the remote can no longer
    write), runs the post-sync as for any lease, then removes the line
    and `sessions/<id>*`. At daemon start
    and on every edit, `kp-sync-session-PROFILE-*` lines of sessions that
    are not live (no `sessions/<id>.json` with a running owner, e.g.
    after a reboot) or that have expired are dropped.
  - `kp-sync sftp-serve ID` refuses unless session `ID` of this profile
    is live, then runs `sftp-server -d /export` (with `-R` for `ro`
    sessions, so read-only is enforced locally) under
    `bwrap --unshare-all --die-with-parent --new-session --clearenv`
    with only `/usr` read-only (plus the `/lib`, `/lib64`, `/bin`
    symlinks), `--dev /dev`, `--tmpfs /tmp`, `sessions/<id>/` bound at
    `/export`, and a one-line `/etc/passwd` and `/etc/group` passed with
    `--ro-bind-data` (no files; `sftp-server` exits with "No user found
    for uid" without them); no `/proc`. sshd starts it through the
    user's shell (`bash -c`, which sources `~/.bashrc` when run by
    sshd): anything that prints to stdout there breaks sftp, which the
    `doctor` self-test catches.
    Without `bwrap`, `session` refuses unless `--unconfined` is given
    (today's behaviour, with a warning).
  Prototyped with OpenSSH 8.9 and bubblewrap 0.6.1 (unprivileged user
  namespaces enabled), against an sshd as permissive as the system one
  (forwarding allowed, sftp subsystem defined), with this machine's
  `~/.bashrc`: sshfs mount, read, write and a rename-over save work; `/`
  shows only the sandbox (no `/home`, `/run` or `/proc`); writes to
  `/usr` fail and writes to `/etc` or `/tmp` land in the sandbox's tmpfs
  and vanish; shell commands only reach `sftp-server`; ptys, `-L` and
  `-R` forwarding are refused; an expired line and a line whose `from=`
  does not match are "Refused by key options". On upgrades, AppArmor's
  user-namespace restriction (Ubuntu 24.04+) may affect `bwrap`; the
  `doctor` self-test catches it.
  The remote side can of course read the database and key file it is
  given; this only stops it from reaching anything else.
  `doctor --self-test` uses a separate `diagnostics/sessions/` namespace:
  an expiring live-owner record, synthetic random challenge and temporary
  SSH key, never a database or key-file copy. Its forced command uses the
  internal `sftp-serve --diagnostic` selector with the same confinement and
  liveness checks. Key sweeps preserve live diagnostic keys, but the daemon
  and engine never discover these exports as database endpoints. A failed
  or timed-out round trip still revokes its key and cleans runtime files;
  if revocation itself fails, report failure and keep the record until expiry.
  With `-w` the remote side edits the
  staged copy in place; with `ro` the staged copy is never a source. While
  a session is open its lease keeps the daemon syncing, so its staged copy
  is updated like any other target during normal daemon syncs; at session
  exit the post-sync picks
  up remote edits. A remote KeePassXC reading the staged copy over sshfs
  is not notified of updates; with `-w` its saves are therefore checked
  as foreign changes.
  Logout reports access/remote cleanup first, then that final
  synchronization is in progress. It reports successful completion only
  after the staged copy and session record have been safely removed;
  an incomplete post-sync explicitly reports retention for daemon retry.
  Successful cleanup also retires the disposable copy identity through
  the crash-recoverable shared-history procedure in "Baseline store".
  A disconnected or expired owner alone is not proof of successful
  synchronization. This applies to normal client completion and daemon
  recovery of dead clients, including named SSH sessions.
- **Named remote services**: a `[ssh_sessions.NAME]` entry starts the same
  confined session lifecycle as `kp-sync session`, but under a transient
  systemd user service with no terminal attached. Its SSH stdin pipe remains
  open while the service owns the mount and lease; terminal output from the
  remote shell is discarded. Stop signals the owner so it can close SSH,
  revoke access, and post-sync. `[access_owners.NAME]` starts a configured
  argv array in a separate transient unit; the owner stays in the foreground,
  receives stop signals, and its stdout/stderr are discarded. Both expose
  kp-sync lifecycle and error messages in the systemd user journal, but do
  not install permanent service templates or alter the daemon.
- **Other machines running `kp-sync`** (e.g. a second laptop): their own
  profile and daemon (`kp-sync service install daemon`, then
  `kp-sync service enable daemon --now`), their own udevmon
  `keydisk` actions calling `attach` / `detach`, KeePassXC through the
  wrapper. See "Machines and pendrives".
- **Phones and other devices**: reach the database only through Google Drive,
  so their edits appear as a cloud change, checked as a foreign change
  (see "Foreign changes" in the sync algorithm) so that a save from an old
  offline cache cannot revert newer edits or bring deleted entries back.
