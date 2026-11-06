# Implementation plan

The authoritative behavior is indexed by [DESIGN.md](DESIGN.md).
This file records current scope and acceptance, not a chronological log.
Superseded descriptions and detailed past reports remain in Git history.
Keep only current status, unfinished work and lessons with continuing value.

## Current status

Release `v0.1.3` is committed as `2e5aab4`, tagged locally on
`offline-pendrive-history` and activated in the default production daemon.
It includes maintenance CLI and cold offline startup fixes; activation and
attached-drive history verification are recorded in NEXT_STEPS.
Release `v0.1.2` remains merged into local `main`. Phases 0-6,
8 and 9 are implemented, including the review/performance follow-ups.
Phase 7 (old-script retirement) is not authorized or complete.

| Area | Acceptance |
|---|---|
| Core, merge, cloud and shared history | Synthetic regression and isolated cloud/exFAT acceptance passed |
| Main-laptop daemon and desktop integration | Deployed; save propagation, physical drive cycles, launch and progress/Hide accepted |
| Second machine (`beta`, now the owner's first laptop) | Installed 2026-11-05 (0.3.4, profile `beta`, persistent ZFS-encrypted hub); owner accepted menu/login launch, edit propagation to `alpha` and both key drives |
| SSH sessions | Read-only refusal, read-write propagation/deletion, cleanup and confined self-test passed with test data |
| kp-access and restore | Test-profile Windows save/END, SSH cleanup and restore preservation passed |
| Production kp-access | BEGIN/open and save propagation accepted; final owner END cleanup and production restore not observed |
| Named SSH/owner services | Implementation and fake-backed tests complete; no live named-service acceptance claimed |
| Optional KeePassXC provider | Deployed; owner approved live pairing/lookup without remembering access; full provider-driven merge acceptance not verified |
| Documentation/API | Strict HTML, offline safety, loopback preview, responsive navigation and source packaging validated |

Release `v0.1.2` validation: 375 tests passed, with Black, all-source
pycodestyle and version consistency checks passing. Provider validation
also passed wheel inspection and safety mutations for password caching
and engine-lock release.

## Completed post-release implementation

### Review-branch service activation management

**Status:** implemented on `review-fixes`, not activated in production.
Approved files-only `service install daemon [--desktop]`, removal of the
`install-units` CLI without an alias, and separate activation through
systemctl-like enable/disable semantics with optional `--now`;
profile-scoped uninstall with optional desktop removal; status reports
activation state separately. Shared templates are conservatively retained,
unsafe/modified/other-profile files are refused, failed shutdown prevents
file removal, and runtime recovery is untouched. The earlier `service-run`
help fix remains included. Validation: 69 focused service, CLI, installation
and documentation tests, Black, all-source pycodestyle, strict Sphinx and safety
mutations for shutdown checking, trigger stop ordering and template retention.
Additional install mutations detect implicit enablement and unrequested
desktop writes. The Python helper keeps its compatible defaults; the CLI
explicitly uses files-only installation.

### Cold offline pendrive startup

**Status:** implemented and verified on `offline-pendrive-history`, released
and activated as v0.1.3; not merged into main.
The owner approved keeping reconciled history and retained bases on every
available configured writable pendrive even when cloud history is online.
Previously cloud reconciliation removed drive history, leaving a fresh
runtime unable to recognize the sole offline copy. Publication now updates
cloud and drive stores, skips identical stores, verifies bases before state
and preserves shared-state race checks and partial-failure reporting.
Unknown-copy and launch safeguards remain unchanged. The authorized
production restart populated the attached drive's history and two retained
bases; runtime/history equality and base hashes were verified. Actual
production reboot/offline acceptance has not been performed.
Validation passed: all 419 tests, Black, all-source pycodestyle, five
documentation tests and strict Sphinx. Coverage includes actual launcher
startup with an empty runtime and unavailable cloud, multi-host offline
merges, replica transfer failure/retry and no redundant cloud uploads.
In-memory mutations reproduced the launch failure with cloud-only
publication and detected a removed shared-state publication check.

### Maintenance CLI extensions

**Status:** implemented on `short-status-cleanup-service`, included in the
activated v0.1.3 release on `offline-pendrive-history`; not merged into main.
Approved scope: collision-aware short text status hashes with `--long`;
metadata-only obsolete-base cleanup with explicit apply scope and
paused-writer confirmation for shared stores; profile-aware daemon service
controls. Cleanup conservatively preserves every version in reconciled
history, leaving graph pruning to normal sync. No production operations are
part of implementation validation. Passed: 414 full regression tests,
51 focused tests after the final status read-error fix, documentation
tests, strict Sphinx, Black and all-source pycodestyle. Safety mutations
detected missing cleanup graph protection, recovery gating and shared-state
rechecks, plus skipped daemon protocol readiness.

### Disposable session record retirement

**Status:** implemented in `873245d`, with failure-path tests in `fa57936`,
and merged into local `main` by `4d562c3`. Schema-v2 terminal markers,
exact-identity legacy confirmation, serialized crash recovery, local/shared
publication outcomes, and pruning behavior are covered by synthetic tests.

Version-1 state migrates in memory; unsupported schemas fail explicitly.
Clients sharing history must be upgraded before the first retirement marker
is published. On 2026-10-28, following owner confirmation that all clients
were upgraded, 11 historical dev01 session identities and the exact legacy
`session:kp-access` identity were retired in runtime history and published
to cloud. Read-only verification confirmed all 12 cloud markers and no
remaining matching copy records. The configured drive history reported
`awaiting publication`; marker receipt there is not confirmed.

### Readable status and drive aliases

**Status:** implemented in the same branch as a separate behavior from
retirement and merged into local `main`. Status groups persistent copies
and sessions, retains technical/JSON identities, reports cached ages and
lifecycle evidence, and uses optional validated display-only drive aliases.

### Documentation cleanup

**Status:** original cleanup and implementation documentation were committed
as `e2c1c7b` and merged into local `main` by `287a362`. `DESIGN.md` indexes
seven chapters, preserving existing decisions.

## Approved, not yet implemented

### Rclone credentials separated from drive attachment

**Status:** contract approved 2026-10-30 and recorded in
`design/endpoints-and-access.md` (Integration points); phases 1-4 are
implemented, merged and released as v0.2.0; alpha migrated 2026-11-03. The
provider lives in udevmon `contrib/rclone-config` (branch
`feature/rclone-config`); kp-sync only consumes. Work on branch
`rclone-drive-sync`; each phase needs its own tests and mutation checks:

1. **Implemented:** `kp-sync drive-sync up|down --mode main|secondary` (identity check,
   pending-up marker under `keydisk.lock`, `--no-wait` submit, marker kept
   on failed submission); `--profile` and `--mode` required.
2. **Implemented** (`rclone_credentials.py`): private rclone copy per
   profile/endpoint, hash-based import only while
   no rclone child runs, "credentials unavailable" status without calling
   rclone; `doctor`/probe use the copy.
3. **Implemented** (daemon tick, not inotify: each tick compares the
   external file's hash): draining and watch (appearance imports and syncs;
   disappearance drains one sync, deleted at run end or user question;
   leftover copy deleted at daemon start). Always resolve paths from the
   current `XDG_RUNTIME_DIR`.
4. **Implemented:** the external rclone deletion and cross-profile scan
   are gone from `_cleanup_runtime`, which drops only private copies; the
   legacy `keydisk.py` hook, its example and tests are removed and the
   second-laptop bundle (retired 2026-11-05) used `drive-sync` plus the
   provider.

Acceptance also covers the action-ordering race (kp-sync `down` accepted
before provider `down`), KeePass-only/rclone-only/combined drives,
preparation failure with offline attach, later credentials, slow
post-sync with rapid reinsert, per-profile copies and crash recovery.
Migration on alpha is complete: provider installed in `~/.venvs/tools`,
udevmon actions switched (provider `down` after kp-sync `down`), legacy
hook retired. `beta` needs no provider: it has its
own rclone configuration, so its udevmon actions are only kp-sync
attach/detach.
Account changes are not detected by design; all rclone-tagged drives must
carry one account.

## Proposed improvements

Collected with the owner on 2026-11-05; not yet approved for
implementation. Each needs its design decisions recorded in the owning
`design/` chapter before work starts.

1. **Cloud staleness warnings.** *Released in 0.4.0:
   `cloud_contact.py`, the launch dialog in `daemon/server.py`,
   `[daemon] cloud_warning_hours`; design in Lifecycle.* `status` reports when the cloud was
   last reached ("cloud last reached 3 days ago"), and a desktop
   notification fires when hub edits have waited for the cloud longer
   than a threshold (about a day). In addition, *always* warn at the
   start with a dialog when the application is about to run without
   cloud access, saying how long it has been since the cloud was last
   reached. Today `confirm_without_cloud` asks only when the hub had to
   be filled from a copy that may be outdated, and otherwise the user
   is only notified. Small.
2. **`kp-sync diff [OLD] [NEW]`.** List the entries and groups added,
   removed and modified between two history versions (default: the
   last two), passwords and protected fields masked, to see what a
   merge or a phone sync changed and to choose a `restore`. Reuses the
   conflict review's masking and comparison code. Medium.
3. **`kp-sync setup HOST`.** A guided setup of a new machine: write the
   host-named profile, read the drive identities, merge the udevmon
   actions, install the units, add the alias and completion, and finish
   with a dry run and `doctor` (the steps of "Setting up another
   machine" in `examples/README.md`). Worth it for more machines or a
   rebuild. Medium to large.
4. **`kp-sync profile rename OLD NEW`.** Automate the `beta`/`alpha`
   renames: refuse while anything is in flight, move the units and the
   configuration, update the integrations (found by `doctor`'s
   integration check, including kp-access's `db-path`), and run the
   first sync. Rare. Medium.
5. **`kp-sync completion install`.** Write the argcomplete registration
   and the alias completion (`examples/bash-completion/kps`) into
   `~/.local/share/bash-completion/completions/`. Small.
6. **Pendrive integrity check.** *Released in 0.4.0:
   `baseline.check_drive_history`, run once per attachment by the
   engine; design in "Missing bases and repair".* On attach, verify the drive's stored
   history files (bases, `state.json`) against their recorded hashes,
   to catch a failing stick before it is the only offline copy. Check
   first which hashes are already recorded. Small.
7. **Pendrive setup.** *Released in 0.4.0:
   `kp-sync drive-setup` (`drive_setup.py`, `udevmon.py`); not yet tried
   on a real new drive.* A command that prepares a new key drive: copy
   the current database (and the history the drive store needs) to it,
   read its serial and filesystem UUID, and add the matching udevmon
   device (tag `keepass`) and, if missing, the attach/detach or
   up/down actions for this profile. Never overwrite an existing
   database on the drive; a drive that already holds one is attached
   and synchronized normally instead. Medium. udevmon 0.7.0 provides
   what it needs: `devices add --from-mountpoint ... --file 50-kp-sync`,
   the kp-sync drop-in and `udevmon trigger`.
8. **Debugging a running daemon for one request.** A client's `--debug`
   asks the daemon to log `DEBUG:` lines for that request only, in the
   journal (today it prints a note that the daemon is not affected).
   Needs a protocol field and a per-request log level in the daemon;
   relaying the lines to the client's terminal is a further option.
   Medium.
9. **Back off the idle sync while offline.** While the cloud is
   unreachable, space out (or skip) the hourly timer's runs instead of
   failing every hour, and return to the normal interval once it is
   reached again. Mostly a quieter journal; it should not delay syncs
   triggered by saves, drives or sessions. Small.
10. **`kp-sync drive-retire DRIVE` (decommission).** *Implemented
    2026-11-06, released in 0.5.0: `drive_retire.py`,
    drive markers in `state.py` (schema 3 only while one exists),
    `SyncEngine.decommission_drive`/`enrol_drive`/exclusion of attached
    decommissioned drives, re-enrolment in `drive-setup`; tests in
    `tests/test_drive_retire.py`.* Rules: "Decommissioned key drives"
    under Baseline store in `design/history-and-sync.md`; command row in
    `design/configuration-cli.md`. Needed for repeatable drive smoke
    tests and lost sticks. Before the first real use, upgrade `alpha` too
    (it cannot read schema 3). Live `kp-test` cycle (setup fill,
    retire, untouched re-attach, re-enrol) passed 2026-11-06, also through udevmon with
    `udisksctl` plug/unplug; desktop
    notice from daemon syncs still open (NEXT_STEPS). Erasing stays a
    separate proposal (per filesystem and medium; revoke a credentials
    drive's rclone token).

Still open from earlier work (see REQUIRED_INPUT and the acceptance
table): retiring old session records safely, retiring the old scripts
on `alpha`, the Windows dialog, and the features not yet seen working
with real data (production kp-access END cleanup, production restore,
named SSH/owner services, provider-driven merge).
11. **Cloud reach check and late offline warning.** *Implemented
    2026-11-06, released in 0.5.0: `reach.py`,
    `RcloneEndpoint._ensure_reachable`, `reach_check`/`reach_timeout`,
    the late warning in `daemon/server.py`, `doctor`'s `reach:NAME`;
    `tests/test_reach.py` and daemon/doctor tests. Live `kp-test` run
    passed 2026-11-06 in an offline network namespace (`unshare -U
    --map-current-user -n`, host network untouched) with a temporary
    `sleep` app: sync 0.15 s (rclone alone >40 s), launch warning
    0.26 s after the request; with `reach_check = false` the app
    started at 20 s unwarned and the late warning followed; online
    sync unchanged (check 26 ms). Follow-up (also 0.5.0): the first
    timeout of a run makes the cloud unavailable for the rest of it
    (design "After a timeout"; tests in `test_reach.py` and
    `test_rclone_rcd.py`); live offline with the check off: one sync
    30.2 s, one timeout).
    NetworkManager is trusted only for `portal`, and proxies follow
    Go's rules (see the design).* Before a run's first rclone call,
    ask NetworkManager and open a TCP connection to the cloud's server
    within `reach_timeout` (3 s); a failure skips the cloud for that run
    as unreachable, a success decides nothing (`reach_check = "auto"`,
    `false` or `"HOST:PORT"` per `rclone` endpoint; `auto` derives the
    server from the remote type, following wrappers and proxies). A
    pre-sync that outlives `prestart_timeout` and ends without the
    cloud shows the launch warning late. `doctor` reports a check that
    fails while rclone works. Rules: "Cloud reach check" in
    `design/endpoints-and-access.md`, the launch steps in
    `design/lifecycle.md`, endpoint keys and `doctor` in
    `design/configuration-cli.md`. Tests: fake NetworkManager, socket
    and resolver (no network); target derivation per type, wrapper
    depth, proxy and `NO_PROXY`; a failed check skips without calling
    rclone and keeps the launch warning; the late warning. Live: a
    `kp-test` launch with the network off (warning before KeePassXC)
    and on (no change). Makes item 9 cheap. Medium.
12. **Configurable reach-check servers.** The rclone type → server
    table of the cloud reach check is hard-coded in `reach.py`
    (`FIXED_SERVERS`, `WRAPPERS`, the `s3`/`webdav`/`sftp`/`ftp` rules).
    A profile table such as `[reach.types] mega = "g.api.mega.co.nz:443"`
    would add or correct types without a release; per-endpoint
    `reach_check = "HOST:PORT"` already overrides one endpoint. A stale
    built-in host hides that cloud on every run until `doctor` flags it,
    so entries should stay few and long-standing. Small; only if a
    provider moves or a new remote type is needed.

## Deferred and supervised work

- Old-script retirement: compare feature parity, then obtain approval
  before changing/removing `keepassdb_ops.sh` or `kp-ssh-access.sh`.
- The nonreproducible Windows dialog remains deferred by the owner.
- Provider-driven merge acceptance, production restore and final production
  owner END cleanup are not proven by a successful lookup or cached status.
- Deployment, further production-history changes and live endpoint checks
  require fresh authorization.
- The test-profile doctor previously misreported missing instance files
  when systemd used installed templates. Confirm current behavior before
  proposing a fix; do not install/enable units merely to clear the report.
- A possible unread `gdbus monitor` stderr-pipe saturation was noted but
  never reproduced; it is not an established defect.

## Rules for every phase

- **Read first:** start at the design index and read applicable chapters.
- **Branch and commits:** use a dedicated branch for implementation work.
  Keep commits focused; merge, tag and push only when authorized.
- **Environment:** use Python from `~/.venvs/tools`; Python >=3.11 is
  supported. Do not install dependencies into the shared environment
  without approval. Black/pycodestyle were approved there; new optional
  documentation/build tooling belongs in an isolated runtime environment.
- **Tests:** use synthetic databases, fake endpoints/keyrings/UI and
  mocked service adapters. Isolate `XDG_RUNTIME_DIR` and notifications.
  Dedicated live acceptance must stay within the test-profile boundaries
  in contributor instructions; production needs fresh approval.
- **Mutation checks:** demonstrate focused regressions detect changes
  that break affected safety invariants. Restore mutations; never mutate
  live production behavior to test a safeguard.
- **Storage and commands:** follow the design's private-runtime storage
  rules and explicit exceptions; commands are argv arrays, secrets never
  enter argv/environment/logs, and diagnostics use INFO:/ERROR: prefixes.
- **Outside this repository:** ask before changing user configuration,
  launchers, hooks, SSH files, installed units or production data.
  Existing dedicated test authorization does not extend to production.
- **Design changes:** agree substantive decisions before implementation,
  record each in its owning chapter, and keep dependent contracts aligned.
- **Completion:** run targeted checks, required full acceptance and review;
  report remaining limitations and update current status without appending
  a historical narrative. Detailed past evidence belongs in Git history.

## Latest implementation validation

The implementation branch passed all 392 unittest cases, Black, and
all-source pycodestyle. Focused in-memory mutations were detected for
retirement precedence, cleanup-evidence gating and baseline pre-write
checks. Additional synthetic tests cover unavailable preview stores,
partial per-store publication and daemon retry after a failed retirement
commit. These checks used synthetic data and fake services; they do not
authorize production migration or deployment.

## Validation commands

From the repository root, with temporary fixtures in runtime storage:

```sh
TMPDIR="$XDG_RUNTIME_DIR" PYTHONPATH=src ~/.venvs/tools/bin/python -m unittest discover -s tests
~/.venvs/tools/bin/python -m black --check src tests examples tools docs
~/.venvs/tools/bin/python -m pycodestyle --config=.pycodestyle src tests examples tools docs
```

Use the smallest relevant test module during implementation. Documentation
changes use `tests.test_documentation` when applicable; strict builds use
`python tools/docs.py build` with an interpreter containing Sphinx.
