# Examples

Templates to copy and adapt; nothing here is installed or applied
automatically. Profiles go to `~/.config/kp-sync/config.toml` (default
profile) or `~/.config/kp-sync/NAME.toml` (`--profile NAME`). The full
key reference is "Configuration" in `design/configuration-cli.md`.
Paths do not expand environment variables: replace `1000` in
`/run/user/1000/...` with your UID (`id -u`).

## kp-sync profiles

| File | Shows |
|---|---|
| [configs/cloud-only.toml](configs/cloud-only.toml) | RAM hub and Google Drive, history in the cloud; the smallest setup |
| [configs/cloud-rcd.toml](configs/cloud-rcd.toml) | `rclone_transport = "auto"` (persistent `rclone rcd`) and credentials published by a key drive (`rclone_config`) |
| [configs/dropbox.toml](configs/dropbox.toml) | Dropbox as the cloud, and how listed hashes affect downloads |
| [configs/pendrives-only.toml](configs/pendrives-only.toml) | no cloud: runtime pendrives with history, drive directory and aliases |
| [configs/persistent-hub-folders.toml](configs/persistent-hub-folders.toml) | hub on an encrypted disk; folder copies that are shared, sometimes mounted or read-only |
| [configs/everything.toml](configs/everything.toml) | every section: two clouds, pendrives, a `command` endpoint, sessions, kp-access, credentials, UI, merge and daemon settings, a custom app |
| [main-laptop.toml](main-laptop.toml) | the main-laptop layout (nested `kp/` folders, cloud and pendrives) |
| [second-laptop.toml](second-laptop.toml) | a second laptop with a persistent hub and attach-only pendrives |

Check a profile without contacting anything:
`kp-sync --profile NAME status` reads cached state only; `doctor`,
`sync` and `history` do contact the configured endpoints.

## udevmon actions

These are udevmon drop-ins (udevmon 0.7.0 or later): copy one to
`~/.config/udevmon/config.d/50-kp-sync.toml`, replace `HOST` and the drive
identities, then run `udevmon check`. kp-sync's file never touches your
`config.toml`; put `[daemon] script_dir` there (a drop-in cannot hold it).
udevmon refuses group- or world-writable configuration files.

| File | Shows |
|---|---|
| [udevmon/main-laptop.toml](udevmon/main-laptop.toml) | `drive-sync --mode main` (drive starts and ends the session); drives tagged `rclone` also need udevmon's `rclone-config` drop-in, whose `order` (10/90) runs it around kp-sync's actions |
| [udevmon/second-laptop.toml](udevmon/second-laptop.toml) | `drive-sync --mode secondary` for a profile named after the host (`HOST`): drives are only attached and detached |
| [bash-completion/kps](bash-completion/kps) | Bash completion for an alias such as `kps` (`kp-sync --profile HOST`); install next to the generated `kp-sync` completion (see the README) |

The `rclone-config` helper and its drop-in (`udevmon.toml.example`, to
`config.d/rclone-config.toml`) live in udevmon's `contrib/rclone-config/`.

## Other files

- [keepass](keepass): `~/bin/keepass` wrapper around `kp-sync run`.

## Setting up another machine

The steps used for `beta` and `alpha`; each needs the owner's approval
on that machine, and nothing here moves or overwrites a database.

1. Install a release tag, not the checkout:
   `~/.venvs/tools/bin/python tools/releases.py install vX.Y.Z --profile HOST`
   (`install --pypi X.Y.Z` takes the release from PyPI instead of a
   checkout; either way it installs missing dependencies such as
   `linux-keyctl` from PyPI first and explains the activation commands
   when no units exist yet),
   and link `~/.local/bin/kp-sync` to the environment's `kp-sync`.
2. Write `~/.config/kp-sync/HOST.toml` (mode 0600) from
   [second-laptop.toml](second-laptop.toml) or
   [main-laptop.toml](main-laptop.toml); check it with
   `kp-sync --profile HOST status` (cached, contacts nothing).
3. With udevmon 0.7.0 or later, plug each key drive and run
   `kp-sync --profile HOST drive-setup MOUNT NAME` (`--dry-run` first;
   `--rclone` for a drive carrying cloud credentials): it registers the
   drive and this profile's actions in kp-sync's drop-in and syncs it.
   By hand instead: copy the matching udevmon example to
   `~/.config/udevmon/config.d/50-kp-sync.toml` with `--profile HOST`,
   keeping only its actions, and add each key drive from the plugged
   drive (`udevmon devices add NAME --from-mountpoint MOUNT --tag keepass
   --file 50-kp-sync`); check with `udevmon check` and
   `udevmon explain mounted`.
   To stop using a key drive (lost, given away, or for a test cycle),
   run `kp-sync --profile HOST drive-retire NAME` to preview, then again
   with `--confirm SERIAL:FS_UUID`; the drive need not be plugged in.
   `drive-setup` enrols it again.
4. Close the application and disable any older startup, then
   `kp-sync --profile HOST service install daemon --desktop` and
   `service enable daemon --now`. Install refuses group-writable
   directories and an executable systemd cannot start, listing every
   problem first.
5. `kp-sync --profile HOST sync --dry-run --no-daemon --ui none`, then a
   real sync; an older hub is merged or, when dominated, backed up and
   replaced, never silently overwritten.
6. Start udevmon (`udevmon service install --now`; if it was running,
   `udevmon reload`, then `udevmon trigger NAME` for a drive that is
   already plugged in), add `alias kps='kp-sync --profile HOST'` to
   `~/.bashrc` and run `kp-sync completion install --alias kps`.

## Starting a new database or adopting an existing one

kp-sync never creates a database. Create it with KeePassXC, put it in one
place, and the first sync records that copy as the first version of the
history, publishes the history and fills the other places.

**A new database** (for example a second one beside `pass.kdbx`):

1. Create it in KeePassXC (Database → New database), with a password and,
   if wanted, a key file. Prefer the GUI to `keepassxc-cli db-create`,
   which writes KDBX 3.1 on KeePassXC 2.6.
2. Give it its own profile and its own places: never the cloud folder,
   history folder or drive folder of another database. For example
   profile `beta-work` with `remote = "GoogleDrive:kp-work"`,
   `[drives] directory = "kp-work"` and its own `hub` and `key_file`
   (`db_name` if the file is not `pass.kdbx`).
3. Put the file in one place: the hub path on a laptop with a persistent
   hub, or the new cloud folder
   (`rclone copyto work.kdbx GoogleDrive:kp-work/pass.kdbx`).
4. `kp-sync --profile beta-work sync`: with no history and one copy, it
   becomes the first version; empty places are filled as they are reached.
5. Continue with "Setting up another machine" (units, `drive-setup`,
   alias).

**An existing database:**

- *One copy:* as above from step 2.
- *Several copies that may differ* (for example kept in step by an older
  script): identical copies simply become the first version, but with no
  history yet every entry that differs between copies is a conflict to
  review. To avoid that, let the first sync see only the copy you trust
  most (other drives unplugged, the old file not in the new cloud
  folder), then add the others one at a time: each is compared with the
  current version and is either backed up and overwritten (nothing newer
  in it) or contributes only its newer entries; old deletions are never
  undone.
- All copies need the same password and key file; a copy with another
  password is asked for separately when it is read.

**The same database on another machine** needs nothing new: point the
machine's profile at the same cloud folder and history (`remote`,
`[baseline] dir`) and follow "Setting up another machine".
