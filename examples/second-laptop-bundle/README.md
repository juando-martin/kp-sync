# Deferred second-laptop acceptance bundle

Assisting agents must first read `AGENTS.md` for staged installation,
approval gates, data-protection rules and local validation instructions.

This is a Git-and-configuration bundle, not an offline installer.
It contains Git bundles of kp-sync, udevmon and the local keyctl dependency,
including their current branch history and tags, plus configuration templates
and launchers. Clone them before following the installation instructions.
`MANIFEST.txt` identifies revisions; `SHA256SUMS` covers the payload.
There are no databases, key files, passwords or rclone credentials.
Do not upload this private bundle to a public service.

Tests on the second laptop are **deferred**, not accepted. Nothing in this
bundle installs itself or modifies the second laptop. Installation and the
production-data checks below require the user to be present.

## Requirements and Python packages

Use a Linux desktop with systemd user services, udisks2 and Python >=3.11.
Required system components: KeePassXC (including keepassxc-cli), rclone,
Zenity, notify-send, libkeyutils, lsblk/findmnt and Python's tkinter.
Install the distribution's Python venv, GI/PyGObject and pyudev packages.
For Debian/Ubuntu, typical package names include `python3-venv`,
`python3-gi`, `python3-pyudev`, `python3-tk`, `libkeyutils1`,
`keepassxc`, `rclone`, `zenity`, `libnotify-bin`, `udisks2`, `util-linux`.
Review package names for the actual distribution before installing.
The bundle needs internet access to fetch remaining Python dependencies.
It does not include architecture-specific system libraries or a wheelhouse.

After extracting, from the bundle root:

```sh
sha256sum -c SHA256SUMS
mkdir sources
git clone repos/keyctl.bundle sources/keyctl
git clone repos/udevmon.bundle sources/udevmon
git clone repos/kp-sync.bundle sources/kp-sync
python3 --version
# Do not replace an existing tools environment without reviewing its users.
python3 -m venv --system-site-packages "$HOME/.venvs/tools"
"$HOME/.venvs/tools/bin/python" -m pip install ./sources/keyctl ./sources/udevmon ./sources/kp-sync
"$HOME/.venvs/tools/bin/python" -c 'import gi, pyudev, keyctl, pykeepass, tkinter'
"$HOME/.venvs/tools/bin/udevmon" --version
"$HOME/.venvs/tools/bin/kp-sync" --help
```

The system-site-packages flag uses the distribution's GI bindings instead of
compiling PyGObject. The keyctl snapshot is the project's existing dependency,
not a similarly named package fetched from elsewhere. The udevmon snapshot
derives its version from its restored Git history and tags.

## Updating later

The clones initially have `origin` pointing at the local `.bundle` file, not
an online server. Keep that file available or configure an approved upstream:

```sh
git -C sources/kp-sync remote -v
# Only after confirming the correct repository URL and access:
git -C sources/kp-sync remote add upstream APPROVED_REPOSITORY_URL
git -C sources/kp-sync fetch upstream
```

No shared network upstream is configured for kp-sync or udevmon on the
preparing host. Do not invent one or copy a private backup address.
Another full bundle can also supply updates without an online remote:

```sh
git -C sources/kp-sync status --short
git -C sources/kp-sync fetch /path/to/new-kp-sync.bundle 'refs/heads/*:refs/remotes/bundle/*'
# Inspect fetched commits, then merge the intended branch with --ff-only.
```

Apply the same workflow to udevmon/keyctl. Preserve local configuration and
uncommitted changes; never reset a clone to force an update. Reinstall updated
packages into the reviewed environment and coordinate daemon restarts after
reviewing changes; fetching or merging alone does not update installed code.
The external `configs/`, README and AGENTS instructions are bundle overlays,
not committed changes in the restored repositories.

## Configure locally, before enabling anything

Back up existing launchers, user units and configurations privately first.
Do not overwrite an existing hub or key file. Never invoke the main laptop's
old synchronization scripts on this machine.

1. Inspect `configs/second-laptop.toml`. Confirm the persistent hub
   (`~/data/pass.kdbx`) and key-file (`~/data/pass.keyx`) paths on this laptop;
   change them if the move into `kp/` also applies here. Both must reside
   on the intended encrypted filesystem. Keep `hub_persistent = true` and
   `require_encrypted = true`. KeePassXC must be fully quit before changing
   its database path. Preserve any existing database, even if it differs.
2. The production remote is `GoogleDrive:kp`, the drive database directory
   is `kp`, and shared history remains `<drive>/kp-sync` / `GoogleDrive:kp/kp-sync`.
   Do not create fresh history or force-copy an old database to initialize.
3. The udevmon-side `rclone-config` provider (package
   `udevmon-rclone-config`, installed into `~/.venvs/tools` with
   `--no-deps` after approval) publishes `<mount>/config/rclone/rclone.conf`
   while a drive tagged `rclone` is present; kp-sync keeps its own private
   copy. In the cloud endpoint table, add
   `rclone_config = "/run/user/LOCAL_UID/rclone/rclone.conf"`, replacing
   `LOCAL_UID` with `id -u` on this laptop (or use its actual XDG_RUNTIME_DIR).
   Do not copy the main laptop's UID or replace an existing rclone config
   symlink. Do not put credential contents in any bundled TOML.
   `~/.dynamic_config` is no longer created or updated.
4. Inspect `lsblk --json --tree -o NAME,SERIAL,UUID,MOUNTPOINTS` and replace
   both identity placeholders in `configs/udevmon.toml`. Add one named device
   with tag `keepass` per approved drive. Do not match every removable disk.
   Merge the device/actions/script directory into any existing udevmon
   configuration rather than replacing unrelated settings.

Install reviewed files manually (these commands refuse an existing profile
or hook/wrapper file; back up and reconcile existing installations first):

```sh
install -d -m 700 "$HOME/.config/kp-sync" "$HOME/.config/udevmon" "$HOME/.local/udev/scripts" "$HOME/bin"
test ! -e "$HOME/.config/kp-sync/second-laptop.toml" &&
  install -m 600 configs/second-laptop.toml "$HOME/.config/kp-sync/second-laptop.toml"
test ! -e "$HOME/bin/keepass-second-laptop" &&
  install -m 700 configs/keepass-second-laptop "$HOME/bin/keepass-second-laptop"
```

The udevmon actions call `kp-sync --profile second-laptop drive-sync
--mode secondary`, which selects **attach/detach**, not main-laptop
**up/down**. No automatic application quit or persistent-hub removal occurs
on detach.

After completing the manual config merge:

```sh
"$HOME/.venvs/tools/bin/udevmon" check
# These steps install/enable real user integration; review before running.
"$HOME/.venvs/tools/bin/kp-sync" --profile second-laptop service install daemon --desktop
"$HOME/.venvs/tools/bin/kp-sync" --profile second-laptop service enable daemon --now
"$HOME/.venvs/tools/bin/udevmon" service install --now
```

For an already running udevmon installation, use `udevmon reload` after the
config merge instead of replacing its service. Review generated kp-sync menu
and autostart entries; do not replace unrelated launchers. The optional
`~/bin/keepass-second-laptop` opens this profile explicitly.

## Supervised acceptance (later)

Start with synthetic data and an isolated test profile/cloud directory,
never production edits just to exercise failure paths. The supplied profile
names the production remote and must not be used as a synthetic test profile.

When ready for the production profile:

1. Mount an approved drive; verify identity, attach lease and runtime rclone
   config permissions (0600). Run
   `~/.venvs/tools/bin/kp-sync --profile second-laptop doctor`.
   Review any encryption/path diagnostic failure; do not disable protection.
2. Run `~/bin/keepass-second-laptop`. Unlock locally, confirm expected
   entries, then make an agreed harmless edit and save. Check
   `~/.venvs/tools/bin/kp-sync --profile second-laptop status`; confirm hub,
   cloud and drive converge with no pending decisions.
3. Safe-eject, unplug and reinsert. Confirm detach/attach and convergence,
   including rapid reinsertion during a slow network sync. On this laptop
   attach does **not** automatically launch KeePassXC; detach does **not**
   quit it or delete its persistent hub.
4. Confirm the waiting dialog appears on slow desktop syncs and closes
   automatically. Existing decisions must remain safe and visible.
5. With both laptops available, supervise an agreed cross-machine edit and
   convergence check. Never overwrite a divergent or unknown copy manually.

Capture status and relevant user journal entries locally:

```sh
journalctl --user -u 'kp-sync@second-laptop.service' -u udevmon.service --no-pager
```

Do not collect passwords, key files, rclone tokens or decrypted database
contents in acceptance logs. Record results in kp-sync's PLAN.md / REVIEW.md.
Phase 4 merge still requires explicit approval.

## Stop / rollback

Fully quit KeePassXC and ensure all edits are synced before stopping services.
Stop/disable the named profile units generated by `service install daemon` (inspect
`systemctl --user list-unit-files 'kp-sync*'`); never disable another profile.
Remove only these reviewed udevmon actions, reload, and restore your backed-up
launchers/configuration if necessary. Do not delete persistent hub data,
baseline stores or runtime recovery copies to clear a warning. Restore no
database from a backup without reviewing which copy contains the latest edits.
