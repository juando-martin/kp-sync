Getting started
===============

Install ``kp-sync`` into the environment that will execute it, with Python
3.11 or later. Linux is required for the daemon, kernel keyring, flock,
and atomic file-promotion operations. External features need their own tools:
``rclone`` for cloud access, ``keepassxc-cli`` for merge verification, and
``ssh``, ``sshfs``, ``bwrap`` and an SFTP server for confined SSH sessions.
Desktop features use a supported display session and configured GUI backend.

Configuration and commands
--------------------------

Use ``man 5 kp-sync-config`` for configuration and ``man 1 kp-sync`` for
workflow and exit statuses. The :doc:`cli` is generated from the installed
source's argparse definitions. A named profile loads
``~/.config/kp-sync/NAME.toml``; no name selects ``config.toml``.

``service install daemon`` writes unit files and reloads systemd without
enabling or starting units; ``--desktop`` adds menu/autostart entries.
``service enable daemon --now`` separately enables and starts configured
activation triggers. ``doctor`` and ``history`` can contact
endpoints. ``sync --dry-run`` still reads endpoints and history. None of
these is a safe way to validate an arbitrary production configuration.

Launch KeePassXC through ``kp-sync run --raise-existing --app keepassxc``
or the installed menu entry. An existing single instance is activated
without a pre-sync; a new instance pre-syncs before being launched through
the daemon. The login autostart does not intentionally steal focus.
Drive-triggered ``up`` still synchronizes even when KeePassXC is running.

Named services
--------------

``service start|stop|status ssh-session NAME`` uses
``[ssh_sessions.NAME]``. The session retains its restricted key and lease
until stopped and keeps incomplete post-sync data for retry.
``service start|stop|status access-owner NAME`` uses
``[access_owners.NAME]``. Owner commands are foreground argv arrays without
a shell; child output is discarded. Lifecycle events go to the journal.
Transient services survive terminal closure, not necessarily logout or reboot.

History and identity
--------------------

An endpoint's ``name`` is a configuration-local alias.
``[baseline].stores`` refers to those aliases locally, not to names shared
between machines. The same physical copy needs a consistent ``identity``
on every machine. File endpoints default to a name-derived identity; cloud
endpoints default to one derived from the configured rclone remote.

``[baseline].dir`` applies to every store in that configuration, relative to
each store's root. Configurations sharing a physical store must resolve to
the same history directory. Renaming a label, remote, or directory does not
migrate history or database files. Do not mix unrelated databases' histories.

Recovery
--------

Never delete pending decisions, displaced copies, or shared history to make
a run succeed. Use ``resolve`` for decisions and ``restore`` for intentional
historical recovery. Runtime state and temporary databases belong on
``XDG_RUNTIME_DIR``; only designated baseline stores and explicitly encrypted
persistent hubs may hold permanent database state.
