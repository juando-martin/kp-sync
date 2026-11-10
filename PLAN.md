# Implementation plan

The authoritative behavior is indexed by [DESIGN.md](DESIGN.md).
This file records current scope and acceptance, not a chronological log.
Superseded descriptions and detailed past reports remain in Git history.
Keep only current status, unfinished work and lessons with continuing value.

## Current status

Release `v0.5.1` (2026-11-10) is current; `beta` runs it and `alpha`
runs 0.5.0 (see NEXT_STEPS for deployment). Phases 0-6, 8 and 9 of
"Implementation phases" in `design/development.md` are implemented,
with the later post-release work: maintenance commands (`cleanup`,
`repair-history`, `service`), cold offline pendrive startup, session
record retirement, readable status and drive aliases, rclone
credentials separated from drive attachment (`drive-sync`, the
provider's private copy), the optional `rclone rcd` transport, and the
proposals released below. Phase 7 (old-script retirement on `alpha`)
needs the owner's approval.

| Area | Acceptance |
|---|---|
| Core, merge, cloud and shared history | Synthetic regression and isolated cloud/exFAT acceptance passed |
| Main laptop (`alpha`) daemon and desktop integration | Deployed; save propagation, physical drive cycles, launch, progress/Hide and the offline launch warning accepted |
| Second laptop (`beta`, the owner's first) | Deployed (profile `beta`, persistent ZFS-encrypted hub); menu/login launch, edit propagation and the key drives accepted |
| Key drives: setup, fill, decommissioning | Live `kp-test` cycles, also through udevmon with `udisksctl` plug/unplug |
| SSH sessions | Read-only refusal, read-write propagation/deletion, cleanup and confined self-test passed with test data |
| kp-access and restore | Test-profile Windows save/END, SSH cleanup and restore preservation passed |
| Production kp-access | BEGIN/open and save propagation accepted; final owner END cleanup and production restore not observed |
| Named SSH/owner services | Implementation and fake-backed tests complete; no live named-service acceptance claimed |
| Optional KeePassXC provider | Deployed; owner approved live pairing/lookup without remembering access; full provider-driven merge acceptance not verified |
| Engine desktop notices (0.5.1) | Unit-tested; not yet seen live |
| Documentation/API | Strict HTML, offline safety, loopback preview, responsive navigation and source packaging validated |

## Proposed improvements

Collected with the owner from 2026-11-05. An open item is not approved
for implementation until its design decisions are recorded in the
owning `design/` chapter and the owner agrees. Numbers stay stable,
since design chapters refer to them; released items keep one line.

1. **Cloud staleness warnings.** Released in 0.4.0 (`cloud_contact.py`,
   the launch dialog, `cloud_warning_hours`; Lifecycle).
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
5. **`kp-sync completion install`.** Released in 0.5.1
   (`completion.py`).
6. **Pendrive integrity check.** Released in 0.4.0 (history bases
   checked once per attachment; Baseline store).
7. **Pendrive setup (`kp-sync drive-setup`).** Released in 0.4.0; it
   fills an empty drive since 0.5.0.
8. **Debugging a running daemon for one request.** A client's `--debug`
   asks the daemon to log `DEBUG:` lines for that request only, in the
   journal (today it prints a note that the daemon is not affected).
   Needs a protocol field and a per-request log level in the daemon;
   relaying the lines to the client's terminal is a further option.
   Medium.
9. **Idle sync while offline.** Released in 0.5.1 (`idle.py`, `sync
   --idle`; "Idle sync while offline" in Lifecycle).
10. **`kp-sync drive-retire DRIVE`.** Released in 0.5.0
    (`drive_retire.py`; "Decommissioned key drives" in History). Erasing
    a drive stays a separate proposal (per filesystem and medium; revoke
    a credentials drive's rclone token).
11. **Cloud reach check and late offline warning.** Released in 0.5.0
    (`reach.py`; "Cloud reach check" in Endpoints, launch steps in
    Lifecycle).
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

- Old-script retirement on `alpha`: compare feature parity, then obtain
  approval before changing or removing `keepassdb_ops.sh` or
  `kp-ssh-access.sh`.
- The nonreproducible Windows dialog remains deferred by the owner.
- Provider-driven merge acceptance, production restore and final
  production owner END cleanup are not proven by a successful lookup or
  cached status.
- Deployment, further production-history changes and live endpoint
  checks require fresh authorization.
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

## Validation commands

From the repository root, with temporary fixtures in runtime storage:

```sh
TMPDIR="$XDG_RUNTIME_DIR" ~/.venvs/tools/bin/python tools/run_tests.py
~/.venvs/tools/bin/python -m black --check src tests examples tools docs
~/.venvs/tools/bin/python -m pycodestyle --config=.pycodestyle src tests examples tools docs
```

Use the smallest relevant test module during implementation. Documentation
changes use `tests.test_documentation` when applicable; strict builds use
`python tools/docs.py build` with an interpreter containing Sphinx.
