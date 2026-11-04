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

Merge these into `~/.config/udevmon/config.toml` (keep unrelated devices
and actions), then run `udevmon check`. udevmon refuses group- or
world-writable configuration files.

| File | Shows |
|---|---|
| [udevmon/main-laptop.toml](udevmon/main-laptop.toml) | `drive-sync --mode main` (drive starts and ends the session) with the `rclone-config` credentials provider, in the required order |
| [udevmon/second-laptop.toml](udevmon/second-laptop.toml) | `drive-sync --mode secondary` for a named profile: drives are only attached and detached |
| [second-laptop-bundle/udevmon.toml](second-laptop-bundle/udevmon.toml) | the second-laptop bundle's version, also with `rclone-config` |

The `rclone-config` helper and its own example live in udevmon's
`contrib/rclone-config/`.

## Other files

- [keepass](keepass): `~/bin/keepass` wrapper around `kp-sync run`.
- [second-laptop-bundle/](second-laptop-bundle/): staged installation
  instructions for a second laptop; read its `AGENTS.md` first.
