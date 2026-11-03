# Second-laptop installation instructions for an assisting agent

## Scope and authority

This bundle prepares a deferred, supervised installation of kp-sync and
udevmon on the second laptop. Read `README.md`, `MANIFEST.txt`, the bundled
configuration templates, and, after cloning locally,
`sources/kp-sync/DESIGN.md` before changing the host. DESIGN.md is authoritative
for synchronization and safety.

Do not interpret opening this bundle as permission to install packages,
replace configuration, start services or synchronize production data.
Perform read-only preflight first; present the concrete local changes and
obtain approval before each installation/activation stage. Keep tests
deferred until the user explicitly agrees to run them.

Do not merge the Phase 4 branch, retire old scripts, modify the main laptop,
or configure remote access as part of this installation.

## Non-negotiable data protection

- Never request passwords or key-file contents in chat, argv, environment
  variables, logs or installation reports. The user unlocks locally.
- Never overwrite or move an existing database to make setup succeed.
  Preserve distinct copies and let kp-sync classify/merge them normally.
- Never delete or recreate shared history, pending state or recovery copies
  to clear a warning. Do not seed history from an arbitrary retained copy.
- The second laptop has a persistent hub on encrypted storage.
  `hub_persistent = true` and `require_encrypted = true` must remain enabled.
- Database snapshots, temporary copies and diagnostic state belong in a
  private directory under this user's XDG_RUNTIME_DIR, not permanent disk.
  The explicitly configured encrypted persistent hub is an exception.
- Private runtime directories are 0700 and files 0600. Do not expose rclone
  configuration or key files while checking permissions.
- Preserve existing services, unrelated udevmon actions, desktop entries,
  autostart items, symlinks and other users' processes. No broad process kills.
- Never run the obsolete main-laptop synchronization scripts against this
  laptop's hub or the relocated production copies.

## 1. Verify and inventory without changing the host

Run from the extracted bundle root:

```sh
sha256sum -c SHA256SUMS
cat MANIFEST.txt
mkdir sources
git clone repos/keyctl.bundle sources/keyctl
git clone repos/udevmon.bundle sources/udevmon
git clone repos/kp-sync.bundle sources/kp-sync
git -C sources/keyctl bundle verify ../../repos/keyctl.bundle
git -C sources/udevmon bundle verify ../../repos/udevmon.bundle
git -C sources/kp-sync bundle verify ../../repos/kp-sync.bundle
id -u
printf '%s\n' "$XDG_RUNTIME_DIR"
python3 --version
command -v keepassxc keepassxc-cli rclone zenity notify-send lsblk findmnt
systemctl --user list-unit-files 'kp-sync*' 'udevmon*' --no-pager
lsblk --json --tree -o NAME,SERIAL,UUID,FSTYPE,MOUNTPOINTS
pgrep -ax keepassxc
```

An absent program, inactive unit or absent KeePassXC process is an inventory
result, not a reason to conceal command errors or assume installation passed.
Clone only into absent destinations; do not overwrite existing repositories.
Bundle verification runs inside each restored repository. Cloning itself
also validates that the full bundle is usable.
Check the desktop session, systemd user manager and udisks2 availability.
Python must be >=3.11. Do not install tools merely to perform this inventory.

Inspect metadata/existence, not secret contents, for:

- `~/.venvs/tools` and which existing packages/launchers depend on it;
- `~/.config/kp-sync/`, `~/.config/udevmon/config.toml`;
- any legacy `~/.local/udev/scripts/keydisk.py` or wrapper;
- `~/bin/keepass-second-laptop`, related menu/autostart entries;
- existing rclone configuration paths and symlinks;
- the proposed persistent hub and key-file paths.

Confirm with the user which encrypted filesystem holds the hub and key file.
Use `findmnt` and `lsblk` to inspect backing storage; do not infer encryption
from the directory name. If verification fails, stop rather than disabling
`require_encrypted`. Obtain the physical serial and filesystem UUID of each
approved drive; do not reuse identifiers from the main laptop blindly.

Explain the planned package environment, path changes, configuration merge,
and service activation. Ask for approval before continuing.

## 2. Install dependencies only after approval

The bundle is not an offline installer. Internet access is required for
unbundled Python/build dependencies; use the local keyctl snapshot rather
than substituting an unrelated index package.

On Debian/Ubuntu, review these candidate system packages with the user:
`python3-venv`, `python3-gi`, `python3-pyudev`, `python3-tk`, `libkeyutils1`,
`keepassxc`, `rclone`, `zenity`, `libnotify-bin`, `udisks2`, `util-linux`.
Use the distribution's package manager only with approval. Other
distributions require their equivalent packages.

If `~/.venvs/tools` already exists, inspect and agree how to reuse it; do not
recreate it, upgrade everything or break other tools. For a new environment:

```sh
python3 -m venv --system-site-packages "$HOME/.venvs/tools"
"$HOME/.venvs/tools/bin/python" -m pip install ./sources/keyctl ./sources/udevmon ./sources/kp-sync
"$HOME/.venvs/tools/bin/python" -c 'import gi, pyudev, keyctl, pykeepass, tkinter'
"$HOME/.venvs/tools/bin/udevmon" --version
"$HOME/.venvs/tools/bin/kp-sync" --help
```

Use system GI bindings through `--system-site-packages`; do not start
compiling PyGObject or changing global Python to work around a missing import.
Report missing prerequisites and resolve them explicitly.

## 3. Adapt configuration before activation

Review every template under `configs/`:

1. `second-laptop.toml` is installed as
   `~/.config/kp-sync/second-laptop.toml`. Confirm `hub` and `key_file`
   locally: `~/data/pass.kdbx` and `~/data/pass.keyx` are examples, not facts
   about this laptop. If the user's move into `kp/` applies here, adapt both
   paths after approval. Do not create or replace database/key files.
2. Preserve cloud remote `GoogleDrive:kp` and `[drives] directory = "kp"`.
   Shared baseline storage remains `GoogleDrive:kp/kp-sync` and
   `<mount>/kp-sync`, not `<mount>/kp/kp-sync`.
3. In the cloud endpoint table, configure `rclone_config` as the absolute
   local path `$XDG_RUNTIME_DIR/rclone/rclone.conf`, expanded to the actual
   path when editing TOML. TOML does not expand shell variables. Never copy
   another machine's UID or put credentials in TOML.
4. `udevmon.toml` has deliberate `REPLACE_FS_UUID` and
   `REPLACE_DRIVE_SERIAL` placeholders. Replace them with verified values.
   Add named devices tagged `keepass` for other approved drives. Merge
   actions and `script_dir` into the existing configuration; do not replace
   unrelated devices/actions or leave placeholders active.
5. The udevmon actions call `kp-sync --profile second-laptop drive-sync
   --mode secondary`, selecting attach/detach, not up/down. Place
   `rclone-config-up` above and `rclone-config-down` below them.
6. The `rclone-config` provider publishes the drive's rclone config and
   kp-sync keeps a private copy; installing the provider into a venv needs
   approval. `~/.dynamic_config` is not used. Do not replace existing
   rclone configuration links or store fresh permanent credential copies.
7. `keepass-second-laptop` explicitly launches the named profile and should
   be installed as `~/bin/keepass-second-laptop`.

Before writing, back up affected non-secret integration files privately in
runtime storage, preserving symlinks as symlinks. Record each exact destination
and whether it was absent or already existed. Never back up a credential file
to an ordinary permanent directory.

Install reviewed config files with mode 0600 and hooks/wrappers with mode
0700; use private parent directories. README.md contains new-file installation
commands. Its `test ! -e` checks are not sufficient for dangling symlinks:
also check `test ! -L PATH` and stop on any existing destination until it is
reconciled. Do not use force-copy or recursive deletion as a shortcut.

## 4. Validate before enabling services

```sh
"$HOME/.venvs/tools/bin/udevmon" check
"$HOME/.venvs/tools/bin/kp-sync" --profile second-laptop status
```

`status` reads cached state; it is not proof that the cloud is reachable or
that the first synchronization is safe. Explain expected empty/pending state.
Fix configuration errors before activation. Do not run `sync`, `resolve`,
`run`, `doctor`, or a drive hook against production data merely as a parser
check: these may access real endpoints or offer real decisions.

Review existing default-profile and named-profile units for conflicts. Ensure
KeePassXC has fully quit before changing its launch path; a closed window can
leave the process running. Ask the user to quit/save; never kill it to proceed.

## 5. Activate only with explicit production approval

Installing/enabling integration may immediately trigger sync (mounted-drive
startup actions, path units, timers and desktop autostart). Obtain permission
for production endpoint access before these commands:

```sh
"$HOME/.venvs/tools/bin/kp-sync" --profile second-laptop service install daemon --desktop
"$HOME/.venvs/tools/bin/kp-sync" --profile second-laptop service enable daemon --now
"$HOME/.venvs/tools/bin/udevmon" service install --now
```

If udevmon is already installed/running, inspect its service and use
`~/.venvs/tools/bin/udevmon reload` for reviewed configuration changes rather
than overwriting the service. Do not enable duplicate monitors.
Inspect generated profile units/menu/autostart entries and confirm their
executable paths point to this laptop's environment.

With approval, mount an authorized drive and run:

```sh
"$HOME/.venvs/tools/bin/kp-sync" --profile second-laptop doctor
"$HOME/.venvs/tools/bin/kp-sync" --profile second-laptop status
systemctl --user status 'kp-sync@second-laptop.service' udevmon.service --no-pager
```

Verify runtime rclone configuration is user-owned and 0600 without displaying
its contents. Missing-cloud, unknown-copy, divergence, password or pending
warnings require normal safe handling; never bypass them by manual copying.
The user enters credentials through local UI only.

## 6. Run only the supervised tests the user authorizes

Follow README.md's acceptance checklist. Synthetic failure/merge experiments
must use an isolated profile, database, key and cloud directory; the supplied
profile points at production and is not a synthetic test profile.

Production acceptance includes opening the correct persistent hub, confirming
entries, an agreed harmless edit/save, convergence across hub/cloud/drive,
safe eject/reinsert including a quick reinsert, and slow-sync waiting-dialog
behavior. Attach on this laptop does **not** automatically launch KeePassXC;
detach does **not** quit it or remove its persistent hub. Do not treat those
differences from the main laptop as bugs.

Cross-machine tests need both laptops and separate authorization. Record
which checks passed, failed or were deferred; do not infer desktop or real
cloud acceptance from unit tests.

For isolated automated tests of kp-sync's source, with prerequisites installed:

```sh
cd sources/kp-sync
PYTHONPATH=src "$HOME/.venvs/tools/bin/python" -m unittest discover -s tests
```

Do not alter the user's real keyring/credentials or add network/GUI operations
to automated tests.

## Future source updates

The sources are real Git clones retaining bundled branch history and tags.
Their `origin` points to the local bundle, not a network upstream. Check
`git remote -v` and `git status --short` before attempting updates.
No online upstream for kp-sync or udevmon is configured on the preparing host;
ask for an approved URL rather than inventing one. Do not copy the keyctl
host's private backup remote into the new installation.

Use an approved upstream (`remote add upstream`, then `fetch upstream`) or
fetch a newer full Git bundle as shown in README.md. Inspect the incoming
commits, preserve local changes, and fast-forward only the intended branch.
Do not reset, force-push or silently switch from the tested Phase 4 branch.
Installing from source is non-editable: after an approved source update,
reinstall the affected package into the reviewed environment, then coordinate
restarting services with the user. Preserve uncommitted configuration overlays
and do not run production synchronization as an automatic update test.

## 7. Report and rollback

Report installed source revisions, destinations changed, service states,
diagnostics, supervised results and remaining deferred work. Keep reports free
of secrets and decrypted database contents. Relevant logs:

```sh
journalctl --user -u 'kp-sync@second-laptop.service' -u udevmon.service --no-pager
```

On failure, stop making changes and explain the error. Preserve every database,
pending decision and recovery file. For rollback, first confirm all edits are
safe and obtain permission to stop only this profile's units. Remove only the
new reviewed udevmon actions, reload, and restore backed-up integration files
without touching unrelated configuration. Do not restore database backups as
part of integration rollback. Phase 4 stays unmerged until explicitly approved.
