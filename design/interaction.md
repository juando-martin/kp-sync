# Applications, interaction and credentials

[Design index](../DESIGN.md)

## Applications (KeePassXC and others)

- **Wrapper**: `kp-sync run [--app NAME] [-- ARGS ...]` asks the daemon
  for the pre-sync, then has it start the application as a transient
  systemd user service with a fixed name (`systemd-run --user
  --unit=kp-sync-app-NAME --collect`, display variables passed). The
  application is thus outside the caller's cgroup (udevmon timeouts and
  restarts do not kill it), and its lease lasts exactly as long as that
  unit is active, followed through systemd's D-Bus signals (no polling,
  no process-name guessing). Applications are described in
  `[apps.NAME]`; `keepassxc` and `keepass-legacy` are predefined.
  `~/bin/keepass`, the desktop menu entry (a copy of
  `org.keepassxc.KeePassXC.desktop` in `~/.local/share/applications/`
  with `Exec=kp-sync run --raise-existing --app keepassxc -- %f`) and the desktop autostart
  entry all go through the wrapper;
  `kp-sync service install daemon --desktop` writes them.
  When starting a new predefined KeePassXC or legacy KeePass instance
  without explicit arguments, pass the configured hub filename; never
  rely on the application's remembered database. Explicit arguments and
  custom application commands are preserved. A no-argument request for
  an already-running single instance still only raises its window.
  A `run` request checks for and activates an existing single instance before
  queuing any pre-sync, without requiring the hub or contacting endpoints.
  Only a new launch follows the pre-sync path; drive-triggered `up` still
  synchronizes even when the application is already running.
  The `run --raise-existing` option forces that raise for a launcher such as
  `~/bin/keepass` even when its stdin is not a terminal; starting a new
  instance still goes through the daemon.
- **Single instance**: KeePassXC passes the arguments of a second launch
  to the running instance and exits. If its unit is already active, the
  wrapper does not start it again: when started by the user (not from udev,
  so focus is not stolen), it raises the window with `wmctrl` by WM_CLASS
  (X11/XWayland) or, if no managed window is listed, forwards the configured
  hub path to KeePassXC's single-instance handler. Explicit file arguments
  are passed to the running instance, and the existing lease continues.
- **Applications started another way** (a terminal, another launcher):
  - KeePassXC is followed through its D-Bus name
    (`org.keepassxc.KeePassXC.MainWindow`): the daemon subscribes to
    `NameOwnerChanged`, gets the owner's pid
    (`GetConnectionUnixProcessID`) and holds a lease until the name goes
    away;
  - other configured applications (`process` in `[apps.NAME]`) are found,
    as a fallback, by process name (`/proc/PID/comm` and `cmdline`; the
    open files are no help, since KeePassXC and KeePass read the database
    and close it) when the daemon starts and at every poll, and are then
    followed with a pidfd.
  This works once the daemon is running (any request, a drive, or a hub
  save through the path unit).
- **Updates of the hub while an application has it open**:
  - KeePassXC with "automatically reload the database when modified
    externally" on (`reloads = true`; the daemon checks
    `~/.config/keepassxc/keepassxc.ini` and warns if the option is off)
    reloads by itself; with unsaved edits pending, KeePassXC offers to
    merge them. No notice is needed. In a readable settings file an absent
    reload key means enabled, matching KeePassXC's default (verified in
    [2.6.6 Config.cpp](https://github.com/keepassxreboot/keepassxc/blob/2.6.6/src/core/Config.cpp));
    a missing settings file remains unverified.
  - An application with `reloads = false` (legacy KeePass, or KeePassXC
    with the option off) keeps the old content in memory. The user is
    notified ("pass.kdbx was updated from the cloud; reopen it in KeePass
    before editing"). Until the application has reopened the file (an
    inotify `IN_OPEN` on the hub after the update, not caused by the
    daemon itself, while that application is the only one tracked; or the
    user's *I reopened it* action on the notification) or exited, its
    saves are treated as
    **foreign changes** (see the sync algorithm), so saving its old
    in-memory copy cannot revert the update or bring deleted entries back.
- **Hub saves**: the daemon watches the hub's directory (KeePassXC saves by
  rename, which breaks a watch on the file), waits `debounce`, and runs
  the full `sync`, so a concurrent cloud edit is merged rather than
  overwritten. Events caused by its own writes are ignored by comparing
  the hub's hash with the one just written. A failed run commits nothing,
  so the hub can stay different from the last committed hash: the daemon
  drops that run's own hub events and records the hub hash it failed
  with, and re-triggers only for a new save or a different hash.
  Otherwise each idle tick would start another visible sync (and progress
  dialog). Silent polls keep retrying at `poll_interval`.
- **Polling**: while a lease is held, endpoints are probed every
  `poll_interval`; the cloud `probe` makes that cheap when nothing
  changed. Without it, a phone edit would be seen only at the next local
  save or at the post-sync.

## User interaction

Requests come from very different places: a terminal, the desktop
(wrapper, menu, autostart), udevmon actions (no stdin, killed with all
their children at `timeout`, 120 s; a udevmon restart kills them and fires
`startup` again), the kp-access hook (must answer an HTTP request quickly)
and ssh logins without a display. None of them does the work itself: they
send a request to the daemon, and in time-limited contexts return at once
(`--no-wait`, implied for `attach`, `detach`, `up`, `down` and
`access-op`). Time limits therefore never cut a sync or a dialog short.

- **One UI layer** (`kp_sync/ui.py`). Every user decision goes through it:
  password, conflict review, 2-way merge without baseline, confirming an
  entry-count drop, launching without the cloud, a change in a read-only
  endpoint, duplicate cloud files, a stale-cache save. Backends:
  - `tty`: prompts in the terminal of the client that made the request
    and is waiting for it; the daemon sends the question over the socket
    and the client answers (a password goes into the keyring, not over
    the socket). Protected conflict values are replaced with `[hidden]`
    before serialization, not merely masked by the receiving terminal;
  - `gui`: shown by the daemon. Simple dialogs (password, questions,
    and file/choice dialogs) use zenity by default or kdialog. A custom
    command is an argv array; each invocation receives one newline-terminated
    JSON request on stdin with `operation` and operation fields (`prompt`,
    `default`, or `options`) and returns one JSON scalar on stdout (boolean
    for confirmation, string for a choice/password/path, or `null` to
    cancel). A choice string must match an option exactly. Exit status 1
    also means cancel; other non-zero statuses are errors.
    Passwords are read only from the dialog's stdout pipe, never from argv.
    The delayed progress dialog remains zenity-only. Conflict review is a
    dedicated tkinter window (Python standard library) for all GUI backends:
    one list of conflicting entries, fields side by side (base / hub /
    other), per-field choice, secrets masked with a show toggle;
  - `none`: never asks.
  Each decision has a safe answer for `none` and for a timed-out dialog:
  defer, overwrite nothing. The exception is "launch without the cloud?",
  whose safe answer is to launch (the hub is usable) and notify.
- **Backend choice** (`--ui` on the client, or `[ui] mode`; default
  `auto`): `tty` if the requesting client has a terminal and waits;
  otherwise `gui` if the daemon has a display; otherwise `none`. The
  daemon takes `DISPLAY` / `WAYLAND_DISPLAY` / `DBUS_SESSION_BUS_ADDRESS`
  from the systemd user manager's environment and, if they are missing
  there, from the first client that has them.
- **Pending decisions**: a decision that cannot be asked now (no UI, the
  client did not wait, *Later*, dialog timeout, locked screen) is recorded
  in `pending.json` (what is needed, which copies, since when), and all
  copies involved are left as they are. The daemon notifies again at most
  once an hour, retries on every later sync, and shows the question as
  soon as a UI is available. `kp-sync resolve` asks it to handle them
  now, on that terminal or on the desktop.
  When no database copy is reachable, synchronization reports
  `unavailable`, not a new reconciliation decision. The daemon suppresses
  endpoint-unavailable and pending-decision notifications until it
  observes a reachable database copy (a local hub or an endpoint observed
  during sync). It keeps existing decisions and normal retry scheduling;
  unavailable endpoints remain in logs and command results. An offline
  boot with an empty runtime hub is therefore quiet, while genuine issues
  notify once a copy is available.
- **Ask before interrupting.** With `ask_first`, the daemon first shows a
  notification or question ("pass.kdbx changed in the cloud and on the
  laptop: merge now?", *Merge now* / *Later*) before asking for the
  password or opening the conflict review.
- **No waiting in the sync slot.** A merge reads its inputs, releases the
  sync slot while asking for the password and conflict choices (other
  syncs, such as promotes and polls, keep running), builds the candidate,
  then takes the slot again and re-probes:
  - inputs unchanged: verify, promote and commit;
  - inputs changed: merge again, reusing the choices already made (keyed by
    entry UUID and the hashes of both versions), so the user is asked only
    about new conflicts.
- **Idempotent triggers.** udevmon fires `startup` again on every restart.
  `attach` of an attached drive and `up` or `run` with the application
  already running only cause a sync.
- **Notifications and progress**: `notify-send` and the zenity progress
  dialog (`progress`) are shown by the daemon, so they also appear for
  requests from udev and kp-access.
  With `[ui] progress = true`, desktop (`auto`/`gui`) user-requested and
  event-triggered syncs show a
  non-cancelling pulsating waiting dialog after two seconds; fast syncs
  do not flash a window. It covers the entire sync, including slow network
  access, and closes on completion or failure, even if the prestart timeout
  has already allowed KeePassXC to launch. It is hidden while a decision
  dialog is open and rearmed afterwards. Explicit `tty`/`none`, headless
  operation and `progress = false` do not show it. Periodic poll-only syncs
  use `ui = none` and do not show a progress dialog. Closing the window does
  not cancel synchronization. A **Hide** button dismisses only the progress
  window (never use Zenity's `--auto-kill`); the compact, multi-line text
  names the profile and configured database path and explains that work
  continues. Display failures are logged, not fatal.
- **Output**: the daemon logs to the journal with level prefixes (`INFO:`,
  `ERROR:`) and to `log` in the run dir; clients print the daemon's reply
  on stderr with the same prefixes, which udevmon and `kp-access-owner`
  keep readable. Secrets are never logged.

## Credentials

- **Optional KeePassXC source (2026-10-28).** When
  `[credentials.keepassxc].url` is configured, an operation needing
  credentials first queries KeePassXC, then falls back to the manually
  populated keyring cache and existing password prompt. The optional
  `keepassxc` package extra supplies `keepassxc-proxy-client` 0.1.7;
  kp-sync has its own small adapter, inspired by ssspwd's proxy flow,
  without an ssspwd dependency. The adapter bounds the transaction to
  `min(ui.dialog_timeout, 60)` seconds (connect at most five seconds),
  frames fragmented JSON with a 1 MiB reply bound, and validates reply
  actions/nonces. It never requests unlocking or launches KeePassXC.
  Silent/headless (`none`) runs skip retrieval because the protocol
  cannot guarantee that entry approval will not display a dialog.
  Optional content hashing never initiates retrieval.
- **Association and selection.** First interactive use requests pairing
  through KeePassXC's normal approval dialog. Existing rejected pairing
  is never silently replaced. Association material (identifier, public
  identification key, database identity and format version; no password)
  persists under
  `$XDG_STATE_HOME/kp-sync/HOST_SLOT/PROFILE/association.json`, falling back
  to `~/.local/state/`. `HOST_SLOT` hashes the configured host, `PROFILE`
  is `default` or `profile-NAME`; each directory is private (0700), the
  file is private (0600), no-follow read and atomically written. This is
  an approved exception to runtime-only storage, not permission to persist
  databases, key files or passwords. A per-association nonblocking flock
  prevents concurrent dialogs. Pairing is pinned to the selected
  database's protocol identity; changing the selected database falls back
  without re-pairing. Search uses only this association's key; normal
  KeePassXC entry permissions remain authoritative. A database identity
  change in a reply aborts lookup.
  Exactly one matching entry is required. Multiple matches explicitly
  warn through the existing interactive choice UI and fall back without
  choosing one. Other actionable connector failures also show a sanitized
  diagnostic before fallback. Prefer a unique hostname such as
  `https://kp-sync-default.invalid`, not paths on a shared hostname.
- **No browser-password cache.** A returned password must open an actual
  observed database with the configured or selected key file before use.
  It is never written to either main or copy keyring slots and is kept
  only in memory for the current sync/restore operation, including
  concurrent-change retries. Lookup occurs at most once per operation,
  and operation credentials are discarded on success or failure.
  Only user-provided passwords are cached. An old manual cache cannot
  override a valid browser password; it remains a fallback even if
  KeePassXC locks or denies access. Master-password entry updates are the
  user's responsibility; this connector never writes entries.
  Browser approval releases the engine flock and daemon sync gate and
  pauses GUI progress. Secrets stay local, including terminal-client
  requests: no returned browser password crosses the daemon socket.
  Normal pre-write revalidation remains mandatory after approval.
- **Forgetting versus disconnecting.** `kp-sync forget` only clears
  manually populated password caches, not browser authorization.
  `kp-sync keepassxc-disconnect` explicitly removes local pairing
  (including a corrupt private regular file); the user must also revoke
  the connection in KeePassXC to remove remote authorization. A later
  interactive credential request may ask to pair again. Locking
  KeePassXC does not revoke a separately cached manual password.
- The password is cached in the kernel keyring through `keyctl`: a `user` key
  named `kp-sync:PROFILE:password` (`default` for the unnamed profile) in the
  user keyring, with a timeout (default 15 minutes, configurable). Profiles
  never share a key. The default profile adopts (and revokes) the
  `kp-sync:password` key of earlier versions once; named profiles never read
  it. The user keyring is shared by all of the user's
  login sessions, including ssh logins. The key gets possessor-only
  permissions (view so `forget` can find copy keys, read/search and
  write/setattr for refresh, timeout and revocation; no access through mere uid match); it is still within reach of
  any process running as the user, a risk comparable to KeePassXC holding
  the unlocked database in memory. Possession requires the user keyring to
  be linked from the process's session keyring: the daemon's unit sets
  `KeyringMode=shared` (systemd links the user keyring into the unit's
  session keyring), and clients link it into their own session keyring
  before writing the key when the login session (`pam_keyinit`) did not.
  `kp-sync doctor` checks that a key written by a client can be read by
  the daemon. Verified on the main laptop (systemd 249): a user service
  with `KeyringMode=shared` (or the user-manager default, `inherit`)
  reads a possessor-only key written from a terminal; with `private` it
  cannot. A client whose session keyring lacks the user keyring cannot
  write the key (`EACCES`) until it links it, after which the daemon
  reads it. The gdm, `login`, `sshd` and `su -l` PAM stacks all run
  `pam_keyinit`.
- It is never put on a command line or in the environment; it is passed to
  `keepassxc-cli` through stdin and to pykeepass in memory.
- If the key is missing or expired, the password is asked through the UI
  layer: in the terminal of a waiting client (which stores it in the
  keyring itself, so it never crosses the daemon's socket) or with zenity
  `--password` shown by the daemon. With no UI available the operation
  stays pending (see "User interaction").
  A password is never asked for "just in case": only when a step actually
  needs it, such as a merge or confirming a drop in entry count.
  A newly entered password is checked against an observed local copy
  before the run relies on it; a rejected one is removed from the keyring
  (`forget`) and asked again, up to three times, then the run fails
  without caching it.
- A copy that opens with neither the cached password nor one learned in the
  run (an old pendrive with a former master key) gets its own key,
  `kp-sync:PROFILE:copy:SLOT`, so its password never replaces the main one.
  `SLOT` is the first 16 hex digits of the SHA-256 of the copy's identity
  (or of its content hash when it has none); its timeout is the cache
  timeout capped at five minutes. In daemon mode the password question
  carries the slot and the client caches the answer there. A cached copy
  password that no longer opens the copy is revoked and asked again; after
  a committed run, which rewrites merged copies with the current key, the
  run's copy keys are revoked.
- `kp-sync forget` revokes the profile's main key and all its copy keys
  immediately.
- The key file is read from its configured path; it is copied only into
  `$XDG_RUNTIME_DIR` (the directories exported to sessions), never
  elsewhere. If it
  is not available (for example because it lives on the absent pendrive)
  and a UI is available, the user is offered a file chooser (a waiting
  terminal client may type a path) for this run only; the path is not
  stored and the file is read in place, never copied. Without a UI it is
  handled like a missing password: steps that need it stay
  pending, promotes without password continue.
