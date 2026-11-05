# kp-sync

`kp-sync` synchronizes one KeePass database across trusted copies such as a
local hub, Google Drive, pendrives, remote `sshfs` sessions, and `kp-access`.
It uses a version history and verified merges rather than file modification
times. KeePassXC should be started through `kp-sync run` so saves are
observed and synchronized.

## Install and configure

Install the package in the Python environment that will run it:

```sh
python -m pip install .
```

Create `~/.config/kp-sync/config.toml`, or use `--profile NAME` to load
`~/.config/kp-sync/NAME.toml`. See `man 5 kp-sync-config` for all options.
Ready-made profiles (cloud, Dropbox, pendrives, folders, the rcd
transport, every option) and udevmon actions are in
[examples/](examples/README.md).
Install and check the user-level daemon integration with:

```sh
kp-sync service install daemon --desktop
kp-sync service enable daemon --now
kp-sync doctor
```

`service install daemon` writes unit files without enabling or starting them;
`--desktop` also writes menu/autostart entries. `doctor` can
probe configured external endpoints, so use these commands only for a profile
whose activation and endpoints you intend to check.

Launch KeePassXC and synchronize through the normal workflow:

```sh
kp-sync run --raise-existing --app keepassxc
kp-sync status
kp-sync sync
```

An already-running single instance is raised immediately without pre-sync.
New instances pre-sync and launch through the daemon; drive-triggered `up`
still synchronizes normally.

The daemon and its private runtime state use `$XDG_RUNTIME_DIR`; the package
does not install or activate production units as part of installation.

## Status and session history

`kp-sync status` shows cached synchronization history without probing
endpoints. It groups persistent copies and sessions separately, reports
recorded synchronization ages, and labels live, unfinished and
historical/unknown sessions. A status name never changes a copy's technical
identity. Optional drive aliases are display-only:

```toml
[drives.aliases]
"SERIAL:FS_UUID" = "Daily backup"
```

Text status uses hash prefixes of at least 12 characters, extended when
needed to distinguish recorded versions. `kp-sync status --long` displays
full hashes; `--json` always preserves full hashes.

`kp-sync retire-session` previews historical session identities from
available baseline history without changing it. The preview may contact
configured history stores. To retire one legacy identity, pass the exact
same identity to `--identity` and `--confirm`; the command rechecks local
session and staging evidence and reports local versus shared publication.
It does not delete database copies. Do not treat a missing remote session
or an unavailable store as evidence that its changes were synchronized.

## Maintenance

Configured writable pendrives keep synchronization history and retained
bases in `kp-sync/`, even while Google Drive is online. This is required for
offline startup after a reboot, when runtime history and the hub are gone.
A drive from an older installation needs one successful sync with recognized
history available first. Do not remove its `state.json` or retained bases
manually; unknown copies still require normal reconciliation.

```sh
kp-sync cleanup                         # preview stores and runtime cache
kp-sync cleanup --store cloud           # preview one baseline endpoint
kp-sync cleanup --runtime --apply       # clean only the local base cache
kp-sync cleanup --store cloud --apply --confirm-writers-paused

kp-sync service status daemon
kp-sync service restart daemon
kp-sync --profile kp-test service restart daemon
```

Before shared cleanup, pause every other writer using the shared history
for the entire operation, including other hosts/profiles. The confirmation
flag asserts this precondition; it does not pause those writers for you.
Cleanup deletes only recognized base files absent from all available
history, without downloading database contents or changing history.
Disconnected copies and retained ancestors remain protected. Pending
decisions, recovery progress and unavailable history block application.
Preview/apply return JSON with candidate bytes, protected/deleted files,
blockers and partial errors. Apply requires one explicit store or runtime
scope, bypasses automatic batching and works with unchanged history.
Google Drive cannot atomically lock history and deletion: do not apply
shared cleanup while other writers may run.

Daemon service controls support `start`, `stop`, `restart` and `status`.
They manage installed default/profile units, verify startup through the
control socket, and do not install units. Stop leaves socket/path/timer
activation enabled, so a later trigger may start the daemon again.
Existing `service start|stop|status ssh-session|access-owner NAME` commands
are unchanged. Custom configuration paths without an installed profile
mapping are rejected rather than controlling the wrong daemon.

```sh
kp-sync service enable daemon          # enable configured installed triggers
kp-sync service enable daemon --now    # also start those triggers
kp-sync service disable daemon        # disable, without stopping
kp-sync service disable daemon --now   # stop triggers, then daemon gracefully
kp-sync service uninstall daemon       # also remove profile unit files
kp-sync service uninstall daemon --desktop  # also remove its desktop entries
```

Enable does not install or rewrite files; use `service install daemon` first.
Status shows each trigger's active/enabled state. Disable is not a permanent
block: explicit requests, desktop/udev launchers and fallback startup can
still start the daemon. Uninstall preserves shared templates, other profiles,
configuration, databases and recovery data. It refuses modified/unsafe files
and leaves drop-ins/vendor units untouched; desktop removal also refuses
entries owned by another profile. Stop issuing requests during uninstall.

## Optional KeePassXC password retrieval

The `keepassxc` extra adds browser-protocol retrieval without depending on
ssspwd. Install `.[keepassxc]` in the environment running kp-sync, then
opt in per profile:

```toml
[credentials.keepassxc]
url = "https://kp-sync-default.invalid"
```

Create one entry with that URL and the database password in an unlocked
KeePassXC database, with browser integration enabled. The URL is a lookup
identifier, not contacted over the network. Choose a unique hostname per
profile. First interactive use asks KeePassXC to associate; name the
connection for kp-sync and approve or deny entry access normally.

When a sync actually needs credentials, it tries KeePassXC before the
manual password cache and existing prompt. Browser passwords are validated
and kept only for that operation, never cached in the keyring. The key
file is still required. Multiple matches warn and fall back without
selecting one. Locked/missing KeePassXC also falls back; kp-sync never
requests unlocking. Silent polls skip browser retrieval to avoid dialogs.
Keep the entry updated when changing the master password.

Private pairing metadata persists under
`$XDG_STATE_HOME/kp-sync/` (default `~/.local/state/kp-sync/`), isolated by
configured host and profile and pinned to the selected database identity.
Selecting another database falls back instead of silently re-pairing.
`kp-sync forget` clears manual caches only. Use
`kp-sync keepassxc-disconnect` to remove local pairing, and revoke the
connection in KeePassXC to remove its authorization there. The next
interactive use can ask to pair again. A cached manual password remains
usable until expiry/forget even if KeePassXC is subsequently locked.

## Named remote services

Named SSH sessions and `kp-access-owner` processes can run independently of
the terminal that starts them. Define them in the configuration:

```toml
[sessions]
enabled = true

[ssh_sessions.office]
target = "user@remote-host:~/kp"
mode = "rw"

[access_owners.windows]
command = ["/path/to/venv/bin/kp-access-owner", "--config", "/path/to/owner.toml"]
```

Each `command` is an argument array and is run directly, without a shell.
Use an absolute executable path when it is not available in the systemd user
manager's `PATH`.
Manage either service with the same interface. These commands open or close
live remote access; do not use them as configuration-only checks.

```sh
kp-sync service start ssh-session office
kp-sync service status ssh-session office
kp-sync service stop ssh-session office

kp-sync service start access-owner windows
kp-sync service status access-owner windows
kp-sync service stop access-owner windows
```

The SSH session mounts the staged database at its configured remote path and
keeps its restricted SSH access and synchronization lease until stopped.
Stopping it revokes the session key, performs the post-sync, and removes
staging only when synchronization completes safely. The owner process runs
in its own transient systemd user service. Both services survive terminal
closure, but are not configured to survive user logout or reboot. Logs go to
the systemd user journal; use the unit name printed by `start` with
`journalctl --user -u UNIT`. For access-owner services, the child must stay
in the foreground and its stdout/stderr are discarded; only kp-sync
lifecycle messages and errors are logged.

## Shell completion

`argcomplete` is installed as a package dependency. Its
`register-python-argcomplete` helper lives next to `kp-sync` in the
environment's `bin` directory (for example `~/.venvs/tools/bin`), which may
not be on `PATH`. For Bash, the simplest is a file that bash-completion
loads on demand:

```sh
mkdir -p ~/.local/share/bash-completion/completions
~/.venvs/tools/bin/register-python-argcomplete kp-sync \
    > ~/.local/share/bash-completion/completions/kp-sync
```

For an alias such as `alias kps='kp-sync --profile HOST'`, also copy
[examples/bash-completion/kps](examples/bash-completion/kps) to that
directory: it completes the alias's expansion. Regenerate the `kp-sync`
file after upgrading `argcomplete`. Alternatively, add the applicable
registration command to your shell startup file:

```sh
# Bash
eval "$(register-python-argcomplete kp-sync)"

# Zsh
eval "$(register-python-argcomplete --shell zsh kp-sync)"

# Fish
register-python-argcomplete --shell fish kp-sync | source
```

The completion parser includes commands, options, service types, and names
from the active configuration.

## Progress window

For desktop synchronization, the delayed Zenity window identifies the active
profile and configured database path. Hiding it does not stop synchronization.
Set `[ui].progress = false` to disable it.

## Documentation

- HTML guides and API reference: `docs/`, built with Sphinx and Napoleon.
  Install optional tools in a separate environment with
  `python -m pip install -e '.[docs,dev]'`; run
  `python tools/docs.py build` or `python tools/docs.py serve`.
  The preview binds to `127.0.0.1:8000`; Ctrl-C stops it.
  Generated HTML lives in ignored `docs/_build/html/`.
  Builds are offline/read-only with respect to endpoints, profiles and
  credentials; they fail on warnings.
- `docs/contributing.rst` describes PEP 257 Google-style docstrings, formatting,
  isolated tests, and warning-free documentation validation.
- `docs/api.rst` identifies the provisional 0.x Python integration surface;
  internal modules do not become supported APIs just by being documented.
- `man 1 kp-sync` describes commands and exit statuses.
- `man 5 kp-sync-config` describes configuration syntax and options.
- `DESIGN.md` indexes the authoritative architecture and safety chapters
  under `design/`, with safety references and task-specific reading guides.
- `PLAN.md` records implementation status and acceptance work.
