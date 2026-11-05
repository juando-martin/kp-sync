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
| Second laptop | `beta` installed 2026-11-05 (0.3.4, profile `second-laptop`, persistent ZFS-encrypted hub); owner accepted menu/login launch, edit propagation to `alpha` and both key drives |
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
   second-laptop bundle uses `drive-sync` plus the provider.

Acceptance also covers the action-ordering race (kp-sync `down` accepted
before provider `down`), KeePass-only/rclone-only/combined drives,
preparation failure with offline attach, later credentials, slow
post-sync with rapid reinsert, per-profile copies and crash recovery.
Migration on alpha is complete: provider installed in `~/.venvs/tools`,
udevmon actions switched (provider `down` after kp-sync `down`), legacy
hook retired. The second laptop (`beta`) needs no provider: it has its
own rclone configuration, so its udevmon actions are only kp-sync
attach/detach.
Account changes are not detected by design; all rclone-tagged drives must
carry one account.

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
