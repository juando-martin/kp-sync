# Agent instructions

Project rules for working on kp-sync. A Git-ignored `CLAUDE.local.md`
may add a developer's own environment (their Python environment,
machines, test profile and approval boundaries); read it when present.

## Status and workflow

`DESIGN.md` is the authoritative design index for the `design/`
chapters; `PLAN.md` owns implementation and acceptance status;
`REVIEW.md` retains useful review lessons and lists open code-review
findings (remove each entry when it is fixed or rejected); `CHANGES.md`
records user-visible changes per release. A release bumps
`pyproject.toml` and `src/kp_sync/__init__.py` together, adds its
`CHANGES.md` section and gets an annotated `vX.Y.Z` tag:
`python tools/releases.py release X.Y.Z "summary"` does all of that on a
clean `main` (`--full-tests` adds the suite). `tools/releases.py install
vX.Y.Z --profile HOST` installs a tag non-editable and refreshes/restarts
that profile's daemon. It first installs the release's missing
dependencies with plain pip (never upgrading satisfied ones; on failure
it stops before touching kp-sync), installs kp-sync itself with
`--no-deps`, and refuses to restart when `pip check` reports kp-sync's
requirements broken. `test vX.Y.Z --report FILE` tests an export and
writes a report, reporting missing dependencies first. With `--pypi`,
both take version `X.Y.Z` from PyPI's source package (checksum
verified) instead of the Git repository; releases reach PyPI by pushing
their tag to GitHub (`.github/workflows/release.yml`).
Documentation tooling is optional (`docs`/`dev` extras). See DESIGN's
Documentation and integration API section for PEP 257/Google docstrings
and the provisional 0.x API policy. Build with
`python tools/docs.py build`; preview with `python tools/docs.py serve`
(loopback only). These commands must not load profiles, use credentials
or contact endpoints. `kp-sync --version` prints the package version
without loading configuration or contacting the daemon/endpoints.

- Start at `DESIGN.md` before proposing or writing anything. Follow its
  chapter index, safety-invariant references and task reading guide.
  Section-name references in existing plans resolve through that index.
- Every decision goes into its owning `design/` chapter (and resolved
  questions are removed from "Open points"); link rather than duplicate
  rules, and keep the index and this file consistent in the same change.
- Implementation follows `PLAN.md` (its "Rules for every phase" apply to
  all work: branch per phase, read the listed sections first, mutation
  checks, nothing outside the repo without asking), which follows
  "Implementation phases" in `design/development.md`. Update `PLAN.md`'s
  status as phases complete.
- Keep work documents current and concise. Superseded descriptions belong
  in Git history, not appended correction logs or parallel archives.
  Retain important lessons, unfinished work and approval boundaries.
- Design chapters include approved but unimplemented behavior. Check PLAN,
  source and tests before treating a design contract as current behavior.
- Maintenance contracts (short status hashes, explicit base cleanup,
  `repair-history` and daemon service controls) live in the
  configuration/commands, history/sync and lifecycle chapters. Check PLAN
  for implementation and validation. Daemon activation enable/disable
  and uninstall preserve other profiles, shared templates and runtime
  recovery; desktop removal is explicit. `service install daemon
  [--desktop]` writes files only; activation uses `service enable daemon
  [--now]`. The old `install-units` CLI is removed.
- Keep reconciled history and retained bases on configured writable
  pendrives even while cloud history is available, so cold offline startup
  can recognize their copies. Never bypass unknown-copy checks to launch.

## Build and test

`pyproject.toml` declares Python >=3.11 (development uses 3.13). TOML
uses stdlib `tomllib`; tests use `unittest`. Packaging uses setuptools
(`setuptools>=77`, for the SPDX `license` field) with the console entry
point `kp-sync = kp_sync.cli:main`. Development checks use Black (79
columns) and pycodestyle (`--config=.pycodestyle`, docstrings/comments
72). Run these from the repository root with a Python that has the
`dev` (and for documentation `docs`) extras, e.g. after
`python -m pip install -e '.[docs,dev]'` in a virtual environment:

- Build a wheel without installing the package:
  `python -m pip wheel --no-deps --wheel-dir /path/to/runtime-output .`
  Use a private output directory under `XDG_RUNTIME_DIR`; isolated build
  dependencies may need network access.
- All tests: `TMPDIR="$XDG_RUNTIME_DIR" PYTHONPATH=src python -m unittest discover -s tests`
  (about 80 s), or in parallel, one process per module (about 40 s; also
  used by `tools/releases.py`):
  `TMPDIR="$XDG_RUNTIME_DIR" python tools/run_tests.py`
- One module: `TMPDIR="$XDG_RUNTIME_DIR" PYTHONPATH=src python -m unittest tests.test_merge`
- One test: `TMPDIR="$XDG_RUNTIME_DIR" PYTHONPATH=src python -m unittest tests.test_merge.MergeTest.test_independent_field_changes_merge_without_conflict`
- Formatting check: `python -m black --check src tests examples tools docs`
- Style check:
  `python -m pycodestyle --config=.pycodestyle src tests examples tools docs`
  `.pycodestyle` permits Black's E203/W503 differences.
- Documentation checks:
  `TMPDIR="$XDG_RUNTIME_DIR" PYTHONPATH=src python -m unittest tests.test_documentation`
  The strict HTML test skips without Sphinx. With the optional docs extra,
  `python tools/docs.py build --output /path/to/runtime-output/html` runs
  a fresh, nitpicky build with warnings treated as errors.
- Tests use stdlib `unittest`, pykeepass-generated fixtures in temp dirs,
  fake endpoints/clock/systemd/D-Bus/UI; never the real cloud, KeePassXC
  GUI, real `~/.ssh` or real databases. The optional localhost SFTP test
  starts an isolated sshd with temporary host keys and authorized_keys;
  it uses loopback networking, not the system sshd. Tests needing
  `keepassxc-cli`, `bwrap` or `sshd` skip when the tool is missing.
  Inject `FakeKeyring` (from `tests/test_credentials.py`) when exercising
  credentials; a real cached password can contaminate synthetic fixtures.
  Set `TMPDIR="$XDG_RUNTIME_DIR"` before the test command when temporary
  fixtures must stay on runtime storage.
- Named-service tests must mock `systemd-run`, `systemctl` and owner
  processes; do not start configured services to validate code.
- `tests/__init__.py` points `HOME`, the XDG directories and
  `XDG_RUNTIME_DIR` at a private temporary directory for the whole run
  (`tests/test_isolation.py` imports it under `discover` and checks it),
  so tests never see real profiles or daemons. Keep it that way: do not
  read `REAL_HOME`/`REAL_RUNTIME_DIR` except to assert isolation. It also
  replaces the cloud reach check's system probes, so no test asks
  NetworkManager or opens a network connection.
- A synthetic `Config` does not isolate the process environment:
  `Controller`, `Client` and `ensure_run_dir` can still read the real
  `os.environ["XDG_RUNTIME_DIR"]`. Patch it to the test's temporary runtime
  before constructing them and assert their directory is the isolated one
  (see `tests/test_access_ops.py`). Mock desktop notification delivery too,
  as in `tests/test_daemon.py`; malformed-pending tests must not notify the
  real desktop.
- `tests.fixtures.kdbx4_bytes` builds structural test data, not an encrypted
  database that PyKeePass can open. Use pykeepass-generated databases for
  credential, merge and real `keepassxc-cli` round trips, created with
  `tests.fixtures.create_database` (pykeepass's function with a light
  Argon2: ~2 ms per open instead of ~0.4 s; the full suite takes about
  80 s instead of 6.5 min). Only the compatibility tests keep KeePassXC's
  default key derivation. Fast runs expose same-second timestamp
  assumptions (KDBX times have one-second resolution): back-date the
  older time instead of relying on slowness. Never reuse live staging
  paths for either kind of fixture.
- Live acceptance uses a dedicated test profile holding only test data
  (the developer's local notes name it), never a real one. Restarting
  services and installing units are acceptable only for that profile and
  temporary synthetic profiles. The opt-in SSH harness is
  `KP_SYNC_TEST_HOST=HOST PYTHONPATH=src python -m tests.acceptance_sessions`
  (`HOST` has `keepassxc-cli` and `sshfs`; reverse tunnel port 2223);
  keep temporary artifacts under runtime storage and verify mount,
  session-key and staged-copy cleanup afterwards.

Verified tool pitfalls (see "Writing with pykeepass" in
`design/history-and-sync.md` and `tests/test_compatibility.py`):

- `keepassxc-cli -q` hides error messages: always judge by exit code.
- `keepassxc-cli db-create` (2.6.6) writes KDBX 3.1; build KDBX 4
  fixtures with `keepassxc-cli import` from XML (`tests/data/keepassxc.xml`).
  From 2.7, `import` sets a password only with `-p`/`--set-password`
  (otherwise "No key is set"); both 2.6 and 2.7 are supported.
- pykeepass has no deleted-objects API (`delete_entry` records nothing):
  write `DeletedObject` elements through `kp.tree`.
- On KDBX 3.1, remove `Meta/HeaderHash` before a pykeepass save, or
  KeePassXC rejects the file.
- A remote `keepass-cli` is a different read-only tool whose documented
  password inputs are argv/environment; do not use it.

## What this project is

`kp-sync` (Python package, TOML config) synchronises one KeePass
database (`pass.kdbx`, used mostly with KeePassXC) between machines,
several pendrives, a cloud remote (rclone) and remote systems (sshfs
sessions, Windows via `kp-access`). It replaces the shell scripts
`keepassdb_ops.sh` and `kp-ssh-access.sh` (see "Existing pieces" in the
architecture chapter).

## Architecture (big picture)

- **Execution boundary**: `cli.py` routes normal commands to the daemon
  client and `--no-daemon` syncs directly to `SyncEngine`. The engine
  orchestrates classification, merge, verification and promotion;
  `history.py` owns DAG operations, `state.py` the serialized records,
  and `baseline.py` reconciliation/publication across stores.
  `daemon/server.py` owns scheduling and lifecycle, with application,
  systemd, D-Bus, inhibitor and rescue adapters behind injectable
  interfaces. `integration.py` renders/installs units and desktop entries
  and implements `doctor`; `examples/` contains external integration
  templates, not automatically applied configuration.
- **Access and restore boundary**: `access_op.py` is the short owner-side
  bridge; `access_ops.py` owns staging validation and upload operations,
  `endpoints/access.py` exposes the staged copy, and the daemon owns
  BEGIN/END leases and sync scheduling. Owner `DB_PATH` is the local staged
  file, while `[access].db_path` and operation paths are virtual HTTP paths;
  the bridge translates between them. Keep the owner command's profile and
  runtime directory aligned: a hub directory named `kp/` does not imply
  `--profile kp`. Restore orchestration lives in `engine.py` and selective
  recovery in `merge.py`; `history_command.py` loads retained baseline
  history and displays versions/backups, so it can contact baseline stores.
- **Hub + endpoints**: each machine running `kp-sync` has its own hub, the
  only place merging happens. The default is `$XDG_RUNTIME_DIR/pass.kdbx`
  (named profiles: `$XDG_RUNTIME_DIR/PROFILE/pass.kdbx`). A hub can
  instead be on an encrypted disk (`hub_persistent`, never deleted by
  `kp-sync`). Machines meet only through the cloud, the pendrives and the
  shared history. Every other location is an endpoint behind one
  interface (`fetch`, `store`, `probe`, `available`, `readonly`,
  `identity`); implemented types are `file`, `rclone`, `command`.
  Runtime-discovered `session` endpoints are implemented too. The engine must
  work for any number of endpoints, including only the cloud.
- **History, not mtimes**: `state.json` is a version DAG (versions with
  parents, files in `bases/<sha>.kdbx`) plus, per physical copy
  (`cloud:…`, `drive:SERIAL:FS_UUID`, `hub:HOST`, `session:…`), the version
  it last held. Copies are compared by SHA-256 (plus a content hash to
  ignore plain re-saves). It lives in `kp-sync/` in the cloud, or on the
  configured writable pendrives for offline use; stores are merged
  record by record (versions unioned, newest record `generation` wins,
  ties by time then host). Pruning contracts the graph so ancestry
  survives.
- **Classification and merge**: one changed copy is promoted; several are
  3-way merged (own pykeepass engine, per field, ancestor = merge-base) and
  verified before promotion. A copy holding an older known version is
  `stale` (overwritten, no password); a known version on another branch
  is `diverged` (merged, never overwritten). A missing runtime hub loses
  its record, so whatever appears there later is `new`. Unknown copies (old pendrives) are
  never promoted, only overwritten if dominated or merged without undoing
  deletions. Changes on `foreign_writers` endpoints (the cloud edited by
  phones, rw sessions) and saves by apps that did not reload an update get
  a descent check (`foreign_check = "always"`); a stale-cache save
  contributes only entries modified after its recorded version.
  Foreign comparisons ignore equivalent missing/default standard fields
  materialized by KeePassXC; real changes still require timestamp checks.
  This includes empty AutoType DefaultSequence and case-equivalent group
  boolean/null settings. Restore defers until changed/unknown copies sync.
  Access END reacquires expired leases and retains unfinished uploads;
  upload creation and END cleanup share access-op.lock.
- **Safety rules spread across sections**: verify without password walks
  the KDBX 4 block chain; pykeepass output is re-opened with
  `keepassxc-cli`; an entry drop above `max_entry_drop`/`min_entry_drop`
  needs confirmation; targets are re-checked before each write; a version
  is committed only once all reachable copies are consistent; unreachable
  endpoints are skipped, not queued (so is a pendrive copy verified
  unchanged after a failed write, then confirmed like drive history); `down` never deletes the only copy of
  unsynced content. Promote writable file endpoints with Linux
  `renameat2(RENAME_EXCHANGE)` and hash the displaced file: remove it only
  if it matches the version read before promotion; otherwise keep it as a
  timestamped runtime backup and leave the run pending without committing
  state. The next run resolves the race marker by merging each unknown
  preserved backup as a `recovered:` unknown copy (never recorded as a
  copy); non-KDBX backups stay pending. Install a
  missing file endpoint with `renameat2(RENAME_NOREPLACE)`. Where
  `RENAME_EXCHANGE` is unsupported (exFAT, the usual pendrive filesystem),
  use displace-then-install: rename the target to
  `.<db_name>.kp-sync-displaced-<random>` in the same directory, install
  the staged candidate with `RENAME_NOREPLACE` (`EEXIST` = concurrent
  save: keep both, restart), then hash the displaced file as above. That
  leftover name on the drive is the crash-recovery record, resolved
  before the endpoint is next probed. Fail safely if `RENAME_NOREPLACE` is
  unsupported; never substitute a check-then-rename-over sequence. This
  protects atomic replace-style saves, not in-place writers.
  Removing a synchronized runtime hub also uses atomic displacement and
  a displaced-hash check, never hash-then-unlink; a concurrent save is
  restored with `RENAME_NOREPLACE` or retained with a pending decision.
  rclone credentials come from a provider-owned file that kp-sync only
  reads; each profile uses its own private copy (`rclone_credentials.py`)
  and never writes or deletes the external file. rclone runs hold
  `rclone-credentials.lock` shared; import/drain/discard hold it
  exclusive. `kp-sync drive-sync` writes a profile/drive-specific
  `keydisk-up-*.pending` marker under
  `keydisk.lock` in the default runtime directory (shared across profiles),
  which reserves the hub and runtime copies until daemon drive
  registration acknowledges it. Preserve this cross-process invariant:
  lifecycle serialization inside the daemon alone does not protect a
  quick reinsert during a slow down/post-sync.
  Old content returns only through `kp-sync restore`
  (`--merge` default, or `--replace`), committed as a new version.
- **On-demand daemon**: one `kp-sync daemon` per user/profile (systemd user
  service, socket activation on `$XDG_RUNTIME_DIR/kp-sync/control.sock`
  with peer-uid check and a small versioned JSON-lines protocol, plus a
  path unit on the hub file); every other command is a short client. It runs
  while **leases** are held: apps started by the `kp-sync run` wrapper as
  `kp-sync-app-NAME` units (or found via KeePassXC's D-Bus name, process
  name as fallback; re-discovered after a daemon restart), attached
  pendrives, sshfs sessions, kp-access `BEGIN`..`END`, pending decisions.
  Applications pre-sync; sessions stage the current hub directly. Application
  launch uses the existing hub after `prestart_timeout`, but if no hub exists
  it waits for the same pre-sync to finish before checking again. Never
  launch KeePassXC on a missing hub path or abandon a launch merely because
  the first download exceeds the fast-start timeout.
  It post-syncs when leases end, syncs on hub saves, polls every
  `poll_interval` without a progress dialog, and exits after
  `idle_exit`. One sync at a time with coalesced triggers; `--no-daemon`
  runs the same engine in-process under a flock. The daemon stays thin
  over the engine (fake clock / systemd / D-Bus in tests).
  Both the daemon sync slot and engine flock are released during user
  questions; reacquisition must revalidate observations before writing.
  Keep `SyncResult.current` aligned with the committed state: the daemon
  uses it to suppress its own hub-write events.
- **User interaction**: all decisions go through one UI layer (`tty` on
  the waiting client's terminal; `gui` shown by the daemon; `none`).
  `ui/factory.py` selects zenity, kdialog or a custom command from
  `[ui].gui`; custom commands use the JSON stdin/stdout protocol in
  `DESIGN.md`. Built-in field-conflict review uses tkinter. Each
  decision has a safe answer (defer, overwrite nothing); undecidable now
  means `pending.json`, retried by the daemon or `kp-sync resolve`. udevmon
  hook requests return without waiting for the sync. `access-op` waits for
  its daemon file operation; BEGIN/END include pre/post-sync, while a
  completed database upload queues synchronization without waiting for it.
  Never block the sync slot while waiting for the user.
  `ui/progress.py` owns the delayed Zenity waiting dialog: desktop syncs
  show it after two seconds, suspend it during questions, and reap it on
  completion/failure. Respect `[ui].progress` and explicit tty/none modes;
  its **Hide** button dismisses only the window, never synchronization.
  Do not use Zenity's `--auto-kill`.
- **Credentials**: password cached in the kernel keyring via `keyctl`
  as `kp-sync:PROFILE:password` (15 min default; default profile adopts the
  legacy `kp-sync:password` once). Alternate-copy passwords use separate
  `kp-sync:PROFILE:copy:SLOT` keys (5 min cap, revoked after commit) and
  must never replace the main key; passed to `keepassxc-cli` on stdin, asked only when a
  step needs it; it never crosses the daemon socket (clients write the
  keyring; daemon unit uses `KeyringMode=shared`). Key file path
  configurable (default `~/data/Personal/pass.keyx`).
  The socket protocol is versioned and limited to 1 MiB per JSON line,
  with strict field/argument validation. Mask protected conflict values
  before socket serialization; a password reply acknowledges local
  caching rather than carrying the password.
- **Optional credential source**: `keepassxc.py` lazily loads the optional
  browser client; `[credentials.keepassxc].url` enables lookup only when
  the engine needs credentials. `_request_credentials` validates its result
  against observed database bytes and the key file before falling back to
  manual cache/UI. `_cache_selected_password` excludes browser-origin
  passwords from every cache-write path. `UI.credential_lookup` runs the
  callback locally outside both synchronization locks, never transports
  its result over daemon IPC, and is skipped by `NoneUI`. Pairing metadata
  is persistent but passwords are operation-only. Connector tests should
  exercise the real optional client's framing/helpers as well as fakes.
- **sshfs sessions**: per-session key, no `ssh -A`, forced
  command `/abs/path/kp-sync --profile P sftp-serve ID` running `sftp-server` under `bwrap`
  with only `sessions/<id>/` visible. The key goes in
  `~/.ssh/authorized_keys` as a `restrict,from=localhost,expiry-time=…`
  line tagged `kp-sync-session-PROFILE-ID`, edited only copy-edit-move
  under a lock, touching no other line; stale lines are dropped by the
  daemon. Never change `sshd_config`.
  Sessions stage the local hub without a pre-sync. The client owns normal
  post-sync; lease disconnect and cleaned-record removal must not duplicate it.
  Staging holds the engine flock and records the session's snapshot ancestry
  only when its exact SHA already exists in the DAG. Unknown hub snapshots
  must retain unknown-copy safeguards, not acquire fabricated versions.
  `doctor --self-test` explicitly opts into a five-minute key and confined
  localhost put/get. Its synthetic exports live under `diagnostics/sessions/`,
  never among database endpoints or leases. Sweeps preserve live diagnostic
  keys; failure still revokes/cleans, while a revocation failure retains the
  record and reports the error.
  Defer hub/session self-write detection while the sync slot is held, then use
  the committed hash so late saves still trigger a run. Do not clear genuine
  queued events. Unchanged history skips publication/prune scans; copy liveness
  refreshes daily (half shorter retention). History-cache reuse requires a
  fresh remote size/checksum match, never metadata timestamps alone. Keep all
  pre-write shared-state checks and failed-session retention.
  Cloud observation is one filtered `lsjson --hash` listing per run; a
  listed checksum identifies known content without download and must
  verify every download (mismatch restarts). Pre-write checks may reuse a
  listing younger than `RECHECK_WINDOW`; post-write checks probe afresh.
  Writes invalidate the listing. Validate a newly typed password before
  relying on it and forget it if rejected.

## Related projects

- `linux-keyctl` (the kernel keyring interface) and `udevmon`: separate
  projects. udevmon 0.7.0 added what kp-sync asked for (drop-ins,
  per-action `order`, `trigger`, `devices add --from-mountpoint`,
  `present --json`, `notify_on_failure`). udevmon is only a client:
  `kp-sync drive-sync` actions in kp-sync's drop-in
  `~/.config/udevmon/config.d/50-kp-sync.toml` pass
  `--drive-id {drive_serial}:{fs_uuid}` (all pendrives mount at the same
  path) and send `up`/`down` (`--mode main`) or `attach`/`detach`
  (`--mode secondary`). `down` runs after the drive is gone; the rclone
  provider's drop-in runs its `up` before (`order = 10`) and its `down`
  after (`order = 90`) kp-sync's actions (default 50) and owns the
  published config.
  This is the current implementation, not a required ownership boundary
  for future integrations. Separating rclone provisioning into udevmon
  requires an agreed provider/consumer lifetime contract and coordinated
  migration; do not remove cleanup/attachment safeguards independently.
- `kp-access`: separate package, used **unchanged**; its `file-ops`
  feature (`PUT`/`DELETE`/`MOVE`, gated only by `--file-op-cmd`) already
  handles uploads. `kp-sync access-op` is that `--file-op-cmd` and must
  re-validate paths itself. Do not move `kp-access` into this repo.
- `keepass-attr` (`kdbx-merge-review`): 2-way tool whose diff/masking/
  conflict UI is reused in `kp_sync/merge.py`; the 3-way merge is new.
  `kdbx-url-sync` stays there.
- A `keepass` launcher wrapper, the KeePassXC menu entry and autostart go
  through `kp-sync run` (`examples/keepass`).

## Conventions

- kp-sync is GPL-3.0-or-later (`LICENSE`). Every Python file and script
  starts (after any shebang) with
  `# SPDX-License-Identifier: GPL-3.0-or-later` and
  `# Copyright (C) 2026 Juan Domingo Martín`; `tests/test_license.py`
  enforces it. Code taken from elsewhere must have a GPLv3-compatible
  license.
- Nothing about a developer's own machines, accounts, drives, paths or
  employer belongs in tracked files, commit messages or identities; it
  goes in Git-ignored local notes. Examples and tests use neutral values.
- Installed releases are the production system; a checkout is not.
  Installing a release and restarting a daemon puts it into production:
  obtain approval before restarting services or testing real endpoints.
  "Setting up another machine" in `examples/README.md` lists the install
  steps; they are instructions, not authorization. Past approvals are
  not continuing authorization.
  Treat `status` as cached state, not proof of endpoint reachability.
  `doctor`, `history`, `sync`, `resolve`, `run` and hooks can contact real endpoints:
  do not use them merely to validate a real configuration.
  Never reset shared history or delete pending/recovery files to bypass a
  decision. Second-laptop attach/detach must not become main-laptop up/down.
- Never write state, backups, temp copies or the key file to a machine's
  permanent storage; use the profile's run dir (`$XDG_RUNTIME_DIR/kp-sync/`,
  or `$XDG_RUNTIME_DIR/kp-sync-PROFILE/` for a named profile; dirs 0700,
  files 0600, atomic write = temp file + fsync + rename). Exceptions: the baseline stores (cloud and configured pendrive replicas) and an
  explicit `hub_persistent` hub on an encrypted disk, plus session lines in
  `~/.ssh/authorized_keys` (public keys only), and private KeePassXC
  association metadata under `$XDG_STATE_HOME/kp-sync/` (default
  `~/.local/state/kp-sync/`; no database/password/key-file bytes).
  An unsynced runtime
  hub is rescued only to a pendrive the user inserts.
  Reuse `paths.ensure_private_directory` and `paths.atomic_write` for
  private runtime state; the latter also fsyncs the containing directory.
  Named-profile hubs default to `$XDG_RUNTIME_DIR/PROFILE/pass.kdbx`,
  separate from their `kp-sync-PROFILE` runtime state directory.
- Every overwrite of a copy is preceded by a timestamped backup and a
  verified candidate; never overwrite silently.
- Named SSH and kp-access-owner services are transient systemd user units
  managed through `kp-sync service`; configured owner commands are argv
  arrays, run in the foreground without a shell, and child stdout/stderr are
  discarded. Lifecycle events and errors go to the journal.
- Never put the password on a command line, in the environment or in logs.
- External commands (rclone, sshfs, zenity) are argument lists, never
  shell strings; cloud transfers use `rclone copyto` (in-place, keeps
  Drive version history).
- Logs to stderr/journal use `INFO:` / `ERROR:` prefixes (udevmon and
  `kp-access-owner` re-log them), plus opt-in `DEBUG:`. Routine
  `log.detail` messages are hidden only for terminal users; see Logging
  in the architecture chapter before changing a message's level. Run
  external programs through `log.logged(runner)` so `--debug` shows
  their command lines; never log stdin or output.
- Follow the sibling projects' layout: setuptools `pyproject.toml` (here
  `setuptools>=77`), `src/` layout, console-script entry points,
  `pykeepass>=4.2`. The engine stays free of daemon code, so
  `--no-daemon` and tests run it directly.
