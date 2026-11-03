# Open review findings

Open code-review findings, most important first. Remove an entry when it is
fixed (the commit records how) or rejected (record why in the owning design
chapter if it is a design decision). Durable lessons belong in
[REVIEW.md](REVIEW.md); acceptance status belongs in [PLAN.md](PLAN.md).

Source: review on `beta` on 2026-11-03. Findings were first made on
`short-status-cleanup-service` (`db06c97`) and re-checked on `main` at
`v0.2.0` (`be12d05`). Line numbers refer to `v0.2.0`. Nothing was changed,
installed or contacted during the review.

**Confirmed** means reproduced or traced through the code end to end.
**Plausible** means traced in code but needing a timing window or an
uncommon configuration.

## Agreed order of work

Round 1 (F1–F3) shipped as `v0.2.1`. Remaining rounds, each on its own
branch with tests, committed locally and pushed only when work moves to
`alpha`:

F4, F6, F7, F8, F9 and F10 are fixed on `main` (unreleased, not pushed);
upgrading Black in `alpha`'s tools environment (F7) is a package install
and needs its own approval there. Acceptance beyond unit tests needs the
`kp-test` profile; installing kp-sync or units on `beta` needs owner
approval, otherwise use `--no-daemon` runs from the checkout.

Remaining, approved on 2026-11-04 for an unattended run (see "Unattended
run" in NEXT_STEPS.md), in this order:

1. **F5** (option 2): take the probe shortcut only when the token
   includes a content hash, where SHA-256, SHA-1 and MD5 all count;
   other rclone remotes and `command` endpoints download every run unless
   an endpoint sets `probe_is_content_hash = true` (default off) because
   its probe prints a real content hash. Document that a custom probe must
   change whenever the content changes. The owner confirmed `alpha` has no
   `command` endpoints; its cloud is Google Drive (hashes listed), so
   production behaviour is unchanged.
2. **F11, part 1**, own branch: batch each run's base uploads, downloads
   and deletes into one rclone call each (`copy`/`delete
   --files-from-raw`), keeping `state.json` and the database as separate
   ordered in-place writes.
3. **F11, part 2**, own branch on top of part 1: the optional `rcd`
   transport as designed below, with `rclone_transport = "cli"` as the
   default and `"auto"` opt-in until it is tested on `alpha`. Open points
   are decided conservatively: an `rcd` failure is handled exactly like a
   failed rclone command (same exceptions, so the existing
   retry/restart rules apply), each operation uses the endpoint's
   `timeout`, and the decision is recorded in the owning design chapter.

## Scope

Read closely (at `db06c97`, re-checked at `v0.2.0`): file and drive
endpoints, the engine's observation, write and commit path, baseline
publication, `cleanup.py`, `services.py` daemon start/stop, short status
hashes, the kp-access bridge and operations, session `authorized_keys`
editing and `sftp-serve` confinement, credential caching and the rclone
endpoint.

Also read: every source change from `db06c97` to `v0.2.0`
(`drive_sync.py`, `rclone_credentials.py`, the daemon credential watch and
draining, rclone endpoint and engine hooks, pendrive history retention, and
`service install|enable|disable|uninstall daemon`).

Not yet reviewed in detail: the unchanged parts of `daemon/server.py`,
`merge.py`, `sessions/session.py`, `config.py` and `integration.py`, and
the UI modules.

F1–F3 (test isolation: real daemon socket, checkout-location and umask
dependence, KeePassXC 2.7 `import`) were fixed on `fix-test-isolation`;
the full suite (443 tests) then passed on `beta`.

## Change detection

### F5. Size and modification time are trusted as content identity (plausible)

`_observe` (`src/kp_sync/engine.py:1489-1513`) skips the download and
treats a copy as its recorded version when the probe token equals the
stored `remote_hashes["probe"]`. That value is stored unconditionally
(`:1405`). For rclone it hashes `{hashes, size, mtime}`. Google Drive
includes MD5, so it is safe. A hashless remote reduces it to size and
modification time, and a `command` endpoint's `probe` can return anything;
`design/architecture.md:80` explicitly allows "hash or size+mtime".

If such a copy is rewritten without its token changing (same size; same
mtime within the backend's precision), the engine treats it as the old
version. A later promotion then backs up the cached base instead of the
real file and overwrites the edit. This conflicts with the REVIEW lesson
that timestamps alone do not establish content identity. Fix: take the
shortcut only when the token includes a content hash, and document that a
command `probe` must change whenever content changes.

## Efficiency

### F11. One rclone call per file dominates cloud history time (proposal)

Each base upload, download and delete is its own rclone call, and the
cost is per call, not per byte. Measured on `beta` against
`GoogleDrive:kp-tests` (rclone 1.60.1, 2026-11-03, read-only downloads
of 4–6 KB files):

| Operation | Time |
|---|---|
| rclone process start without network | 0.03 s |
| one `copyto` from `kp-test-history/bases/` | 4.0 s |
| three files, three `copyto` calls | 12.0 s |
| three files, one `copy --files-from-raw --no-traverse` call | 2.4 s |
| per-run `lsjson --hash` listing (today) | 1.1 s |
| persistent `rclone rcd`: listing / one download / three downloads | 0.27 / 0.9 / 2.9 s |

The time is Drive authentication and path lookups per call, not process
start. A commit currently makes about eight sequential `copyto` calls at
3–4 s each (daemon logs, F9 live test).

Proposal: batch content-addressed base uploads, downloads and deletes of
one run into one rclone call each (`copy`/`delete --files-from-raw`).
Keep `state.json` and the database as separate in-place `copyto` writes:
their order (bases before state) and the shared-state re-checks between
them are safety rules. Batching comes first; it works on rclone 1.60.1.

Optional fast transport (owner agreed the design on 2026-11-03; approved
for implementation on 2026-11-04, see "Agreed order of work"): a
persistent `rclone rcd`, used only when available and plain rclone calls
otherwise.

- *Requirement.* `rcd` listens on a Unix socket only from rclone 1.61
  (verified: 1.60.1 fails with `listen tcp: … unknown port` although its
  man page documents it). The socket lives in the profile run dir (0700),
  so access is limited to the user, as with the private config copy;
  `rcd` then runs with `--rc-no-auth` (see below). No loopback TCP
  fallback.
- *Selection.* Probe by starting `rcd` on the socket and checking that it
  answers, not by parsing versions. Setting such as
  `rclone_transport = "auto" | "cli"`; endpoints with custom
  `download`/`upload`/`probe` commands always use them.
- *Credentials (F8).* `rcd` reads its `--config` once, keeps credentials
  in memory, rewrites (and recreates) the file on token refresh and has
  no reload call. It therefore holds `rclone-credentials.lock` shared for
  its whole lifetime: import and drain stop it first, take the lock
  exclusive, then restart it if credentials remain. The daemon owns it
  and stops it on drain and idle exit; other processes use its socket
  when healthy and plain rclone (same shared lock) otherwise.
- *Verified on `beta` with rclone 1.75.2 (2026-11-04,
  `GoogleDrive:kp-tests`).* `rcd --rc-addr unix://…` works; the socket is
  created 0775, so it must live in a 0700 directory. Contrary to the man
  page, the socket does not bypass rc authentication for `operations/*`
  (HTTP 403) unless `--rc-no-auth` is set, which is acceptable only on
  such a private socket. One `operations/list` with `_filter` include
  rules, `recurse` and `showHash` returns exactly kp-sync's per-run
  listing (database, `state.json`, bases) with MD5, SHA-1 and SHA-256.
  `operations/copyfile` uploads update the Drive file in place (same file
  ID, like `copyto`, so revision history is kept). The socket is removed
  when `rcd` stops. Still open: per-operation timeouts and mapping a
  half-done `rcd` operation onto the retry/restart rules.
- *Measured gain (rclone 1.75.2).* Filtered recursive listing 1.84 s
  (CLI) → 1.09 s (`rcd`, after a 1.8 s first call); database download
  2.70 s → 1.02 s; in-place upload via `rcd` 1.74 s. Listing gains less
  than the earlier non-recursive 0.27 s because Drive lists each folder.

## Reviewed without findings

- File and drive promotion: `RENAME_EXCHANGE`, displace-then-install on
  exFAT, `RENAME_NOREPLACE` preflight, displaced-file recovery and drive
  identity rechecks around writes.
- Engine writes: backup and recheck before each write, in-flight candidate
  records before partial promotion, commit only after final confirmation.
- `cleanup.py`: whole-graph protection, recovery blockers, per-deletion
  rechecks, explicit scope and paused-writer confirmation.
- `services.py` daemon start/stop: config-path refusal, clean-shutdown
  verification and protocol readiness.
- kp-access: virtual path validation, private upload staging, KDBX checks
  before installing the database and backups before replacement.
- Sessions: `authorized_keys` editing touches only tagged lines, under a
  lock, with an identity check; `sftp-serve` uses bwrap confinement by
  default and needs an explicit opt-out.
- Credentials: keyring access goes through the `keyctl` library, never
  argv; keys are secured or revoked on failure.
- Drive role selection (fixed in `v0.2.0`): `drive-sync` requires
  `--mode main|secondary`, and `~/.dynamic_config` is no longer used.
- `drive_sync.py`: identity verified against the mounted filesystem before
  the pending-up marker is written under `keydisk.lock`; the marker is
  kept when submission fails; `--config`/`--profile` are mutually
  exclusive, so the right daemon is addressed.
- `rclone_credentials.py` itself: the external file is only read
  (`O_NOFOLLOW`, size cap, regular-file check); the private copy is written
  atomically with private modes, and symlinks are refused.
- Daemon activation controls: `enable` requires installed units;
  `disable`/`uninstall` stop triggers before the daemon and verify a clean
  shutdown; `uninstall` removes only unmodified generated files after a
  preflight and rechecks each file before unlinking it.
