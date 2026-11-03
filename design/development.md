# Development and implementation planning

[Design index](../DESIGN.md)

## Testing

- The dedicated `kp-test` profile is available for explicitly scoped
  integration acceptance with test-only databases and endpoints, including
  SSH/sshfs entry edits using `keepassxc-cli` without GUI interaction.
  Operational paths and credential handling are documented in
  `.github/copilot-instructions.md` and `NEXT_STEPS.md`. Passwords are supplied
  through stdin, never argv/environment or the shared production keyring.
  This does not change the isolated, fake-backed unit-test policy or authorize
  production access and service restarts.
- Unit tests with pykeepass-generated fixture databases for the 3-way merge
  rules (each case in the merge list above), classification and
  verification.
- Engine tests with fake `file` and `command` endpoints in a temporary run
  dir: one change, several changes, stale and missing endpoints, endpoint
  returning after missed syncs, concurrent change between fetch and store,
  missing hub, change of master key, per-record merge of `state.json` from
  two stores, truncated KDBX 4 candidates, `GET` not overwriting an upload,
  `down` keeping an unsynced hub, a hub recreated after a reboot classified
  `new` (never promoted), a large size drop asking for confirmation,
  displace-then-install on a file endpoint whose filesystem rejects
  `RENAME_EXCHANGE` (simulated; optionally on a real exFAT image when one
  can be mounted without privileges): a normal promotion, a writer
  recreating the target in the gap (both versions kept), a racing save
  displaced (backup and pending), recovery of each leftover state (target
  absent, displaced equals the recorded version, displaced differs), and
  `RENAME_NOREPLACE` also unsupported failing without touching the target,
  the
  kp-access `PUT`/`DELETE`/`MOVE` sequence (no refill or sync while in
  transit).
- Multi-machine and drive tests with two simulated hosts sharing one fake
  cloud and store: merge-base across hosts, a drive edited on host B and
  attached on host A, a stale drive updated without password, an unknown
  outdated drive (dominated, overwritten) and an unknown drive with newer
  entries (merged, no resurrected deletions), two drives attached at once,
  pruning keeping common ancestors, a stale copy still recognised after
  the versions in between were pruned (and after merging a store that
  still holds them), two branches committed offline to different stores
  (`diverged`, merged, same current version chosen on both hosts),
  persistent hub never deleted, online synchronization seeding an empty
  drive history without redundant cloud uploads, and a cold offline reboot
  launching from that drive with neither a runtime hub nor cached history.
- Restore tests: `--merge` bringing back a deleted entry without removing
  newer ones, `--replace` not being reverted by a stale copy or a later
  merge, a restored version never classified as `stale`.
- Foreign-change tests: a cloud edit descending from its recorded version
  (promoted), a stale-cache save that adds one entry and lacks newer edits
  and entries (only the added entry taken, nothing reverted or deleted), a
  deletion made on the phone (applied), skewed phone clock (reported).
- Optional compatibility tests (skipped if `keepassxc-cli` is not
  installed): a merge result written by pykeepass opens in `keepassxc-cli`;
  a fixture written by KeePassXC itself (built with `keepassxc-cli import`
  from an XML file: tags, custom icons, `CustomData`, browser-integration
  keys, history, attachments, deleted objects; KDBX 4.1 fields such as
  icon names once a KeePassXC that writes 4.1 is available) loses
  nothing when opened and saved by pykeepass (`keepassxc-cli export`
  before and after compared, ignoring `CustomData` order and its
  `_LAST_MODIFIED` stamps); deletions written by the merge are kept; a
  KDBX 3.1 file saved by the merge opens in KeePassXC. This fixture is created once and kept in
  `tests/`.
- UI layer with a scripted fake backend: choosing the backend per request,
  tty prompts routed to the waiting client, safe answers under `none` and
  on dialog timeout, `pending.json`, reuse of conflict choices after
  inputs change.
- Daemon tests (in-process, socket in a temporary dir, fake clock, fake
  systemd and D-Bus, fake applications): pre-sync and post-sync, idle
  exit, a dropped connection releasing its lease, a second launch of a
  single-instance application keeping the existing lease, an application
  started without the wrapper followed through D-Bus or by process name,
  application leases taken again from active units after a daemon
  restart, a kp-access lease expiring without removing `staging/`, protocol
  version mismatch, idle-sync timer, inhibitor taken and released, rescue
  offer and rescue file on a foreign drive, coalesced triggers,
  restart restoring named leases, saves of a non-reloading application
  after an update treated as foreign changes, peer uid check.
- Session key tests on a temporary `authorized_keys`: other lines kept
  byte for byte (comments, options, no trailing newline), only
  `kp-sync-session-*` lines of the right profile added or dropped,
  refusal of a symlink or group-writable file, restart when the file
  changes between read and replace, stale and expired lines dropped, a
  crashed session client leading to post-sync and line removal.
  Optional (skipped without `bwrap` or `sshd`): an unprivileged sshd on
  a localhost port reading that file, checking the line options and the
  `bwrap` sandbox as in the prototype.
- The default unit suite never uses a real cloud, real `authorized_keys`
  or KeePassXC GUI. The optional localhost sshd test uses only temporary
  keys/configuration and executes the actual confined SFTP put/get.
- `PYTHONPATH=src python -m tests.acceptance_sessions` is a separate opt-in
  acceptance run for the supplied `kp-test` infrastructure. It checks the
  configured test paths, uses remote `~/kp-test` and tunnel port 2223, verifies
  read-only refusal and read-write CLI entry propagation/removal, and checks
  mount/key/staged-copy cleanup. Credentials stay in-process/on stdin, never
  in the shared keyring. Diagnostic exports avoid interference from an older
  running test daemon; direct engine runs still share the profile flock.
  It writes a non-secret timing/result report under the test profile's runtime
  directory. Live daemon activation/progress behavior remains a separate check.

## Documentation and integration API

- **Source documentation:** PEP 257 module/class/meaningful public-operation
  docstrings, Google-style sections rendered by Sphinx Napoleon. Document
  parameters, returns, errors, side effects, blocking I/O, locks, credential
  boundaries and runtime-storage requirements where relevant. Do not add
  boilerplate to trivial internals. PEP 8 limits maintained Python to 79
  columns, docstrings/comments to 72; prefer parenthesized continuations.
  Black and pycodestyle are optional development tools, with E203/W503
  exceptions for Black compatibility. Formatting must preserve behavior.
  Maintained sources include src, tests, examples, tools and docs Python;
  throwaway spikes and archived bundles are outside this formatting scope.
- **HTML documentation:** `docs/` uses Sphinx's classic theme, autodoc,
  Napoleon and viewcode; `tools/docs.py build|serve` makes a fresh, nitpicky
  build that fails on warnings. Preview is loopback-only and stops on Ctrl-C.
  A local stylesheet separates the sidebar and content into grid columns,
  wraps long API names/signatures, and stacks navigation below the content
  on narrow screens so the index cannot overlap the reference.
  On wider screens the sidebar stays at the viewport top during content
  scrolling and has its own vertical scrollbar; following reference anchors
  preserves access to navigation. Narrow screens retain normal page scrolling.
  Generated HTML is ignored, never committed. Sphinx is an optional `docs`
  dependency; development tools are an optional `dev` extra. Install them
  only in an explicitly isolated environment, not the shared production venv.
  After dependency installation the build needs no network. Imports and
  reference generation never load user configuration, start a daemon/service,
  invoke endpoint operations, access a keyring or run live examples.
- **Single source:** API reference is generated from docstrings; CLI help
  comes from the actual argparse parser without calling `main`. Man pages
  retain workflow/configuration context. README, man pages, examples, PLAN,
  handoff and this design must reflect current behavior; old acceptance
  records remain historical evidence rather than new deployment approval.
- **Integration surface:** prefer CLI argv for process integrations and
  `daemon.client.Client`/`LeaseConnection` for Python client requests.
  In-process integrations can deliberately use `config.load_config` and
  `Config`/`EndpointConfig`, `engine.SyncEngine`/`SyncResult` and
  `endpoints_from_config`, the Endpoint/fingerprint/error contracts and
  built-in endpoint classes, the UI/NoneUI and CredentialProvider contracts.
  The qualified names listed in `docs/api.rst` are provisional during 0.x:
  callers must pin/test versions; documenting internals is not a stability
  promise. No new top-level exports or runtime APIs are introduced here.
  Private helpers, controller internals, XML merge details and serialized
  state formats are not public extension APIs. Never edit state/pending
  files as an integration mechanism.
- **Completeness review:** command endpoints and direct Python endpoint
  injection already permit new transports; custom JSON GUI and kp-access
  file-op adapters cover existing interaction needs. No additional runtime
  API is justified in this phase. A versioned endpoint factory/plugin registry
  (custom TOML types), typed client replies/event subscription, and a separate
  custom baseline-store adapter are future proposals only: require a real
  caller, compatibility tests and safety design before implementation.
  A custom endpoint does not automatically become baseline-capable.
- **Verification:** isolated synthetic tests, strict HTML build, parser-help
  checks, offline build guards, loopback preview smoke test, style checks,
  syntax/AST comparison excluding docstrings, and wheel inspection. Source
  formatting and documentation do not authorize any production restart,
  endpoint operation, deployment or installation into the shared venv.

## Implementation phases

1. Core library: config (including persistent hubs), version history and
   merge-base, endpoints (`file`, `command`), drive instances, hashing,
   classification, verify, promote, backups, lock, logging.
2. UI layer (`tty`, `gui`, `none`), 3-way merge engine
   (`kp_sync/merge.py`), conflict review (tkinter and terminal), keyring-cached credentials,
   content hash, unknown-copy check, foreign-change check, `pending.json`,
   `resolve`.
3. `rclone` endpoint with probe; baseline stores (cloud, pendrive replicas,
   reconcile); `status`, `sync`, `--dry-run` (in-process, `--no-daemon`).
4. Daemon: displace-then-install for file endpoints without atomic
   exchange (exFAT pendrives; first, since drives sync from here on),
   socket and path activation, leases, triggers, polling,
   application units and D-Bus tracking, idle timer, inhibitor, rescue,
   `run`, `up`, `down`, `attach`, `detach`, `eject`, `service install daemon`,
   `doctor`; changes to `keydisk.sh`,
   `~/bin/keepass` and the menu entry; profile and units for the second
   laptop.
5. sshfs sessions (per-session keys in `authorized_keys`, `sftp-serve` under `bwrap`).
6. `kp-sync access-op` and switching `kp-access-owner` to it; `restore`
   and `history`.
7. Retire `keepassdb_ops.sh` and `kp-ssh-access.sh`.
8. Named service controls for SSH sessions and `kp-access-owner`, an
   identifying progress window, shell completion, README, and man pages.
9. Documentation build/preview, integration API contracts, meaningful
   docstrings, PEP 8 formatting, and consistent implementation references.

## Open points

- None at the moment.
