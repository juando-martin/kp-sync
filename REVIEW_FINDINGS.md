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

F8, F9 and F10 are fixed on `main` (unreleased, not pushed). Acceptance
beyond unit tests needs the `kp-test` profile; installing kp-sync or
units on `beta` needs owner approval, otherwise use `--no-daemon` runs
from the checkout.

1. **F4** (owner chose the recommended option on 2026-11-03; start only
   when asked): upload missing bases from the fresh listing; when a base
   exists nowhere, keep refusing to publish, name the version, and add an
   explicit repair command rather than dropping versions automatically
   (they may be merge ancestors).
2. **F7** (owner chose option a on 2026-11-03): move both laptops to
   Black 26 and reformat in a separate formatting-only commit; widen the
   Sphinx pin only if the strict build passes under 9.2. Upgrading Black
   in `alpha`'s tools environment is a package install and needs its own
   approval there.
3. **F6** (owner approved fixing it on 2026-11-03): also read the
   KeePassXC setting under `XDG_CONFIG_HOME` and the Flatpak config path.
4. **F5** (owner chose option 2 on 2026-11-03; do not implement yet):
   take the probe shortcut only when the token includes a content hash;
   other rclone remotes and `command` endpoints download, unless an
   endpoint opts in (e.g. `probe_is_content_hash = true`) because its
   probe prints a real content hash. Document that a custom probe must
   change whenever the content changes.
5. **F11** (proposal and optional `rcd` design recorded on 2026-11-03;
   do not implement yet): batch per-run rclone transfers of history
   bases, then optionally a persistent `rcd` transport on rclone ≥ 1.61.

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

## History publication

### F4. A missing remote base blocks cloud publication (plausible)

`BaselineManager.save` requires a reachable base for every retained version
(`src/kp_sync/baseline.py:430`) but uploads a base to a store only when its
version is absent from that store's recorded state (`:440-442`). Nothing
restores a base that is listed in a store's state but missing as a file.
Runtime base caches are pruned to retained versions and lost at logout.

Since `v0.1.3`, configured pendrives also keep retained bases, so
`ensure_base` can find a base missing from the cloud while such a pendrive
is attached. The cloud copy is still never re-uploaded, though: the failure
("cannot publish history: base … is unavailable") returns on every host
without a cached copy whenever no pendrive holding the base is attached.
The same applies to a base missing from a pendrive store.

One way to get there, with no cross-host lock: host A publishes, re-checks,
then deletes stale bases from the listing taken by that check
(`:451-457`). If host B checked before A's publish and writes its state
between A's re-check and A's deletes, A can delete a base that B's
just-published state references. The window is several seconds for the
cloud, because pruning deletes eight or more files one rclone call at a
time. Manual or Drive-side deletion has the same effect.

Fix: decide uploads per store from the fresh base listing that `check`
already fetches, not from recorded versions. That also fixes the cost
below. Consider whether a retained version whose base exists nowhere should
block all publication or be dropped from the graph with a diagnostic.

Related efficiency issue: after a reboot, the first publish calls
`ensure_base` for every retained version and downloads every uncached base,
even though only bases missing from a target need uploading.

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

Optional fast transport (owner agreed the design on 2026-11-03; do not
implement yet): a persistent `rclone rcd`, used only when available and
plain rclone calls otherwise.

- *Requirement.* `rcd` listens on a Unix socket only from rclone 1.61
  (verified: 1.60.1 fails with `listen tcp: … unknown port` although its
  man page documents it). The socket lives in the profile run dir (0700),
  so access is limited to the user, as with the private config copy;
  sockets bypass rclone's own authentication. No loopback TCP fallback.
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
- *To verify first.* `operations/copyfile` updates in place like
  `copyto` (Drive revision history); `operations/list` can do the
  filtered `--hash` listing in one call; per-operation timeouts and how a
  half-done `rcd` operation maps onto the retry/restart rules.
- *Expected gain.* Listing 1.1 → 0.27 s, each download 4 → 0.9 s after a
  ~1 s warm-up; combines with batching. Not considered further: the Drive
batch API directly (drops rclone), `librclone` (not packaged by Debian),
`rclone mount` (cache consistency) and archiving the bases (changes the
history layout).

## Minor

### F6. KeePassXC reload setting read from one fixed path (confirmed)

`src/kp_sync/daemon/apps.py:261` and `src/kp_sync/integration.py:704`
read only `~/.config/keepassxc/keepassxc.ini`. A non-default
`XDG_CONFIG_HOME` or a Flatpak KeePassXC
(`~/.var/app/org.keepassxc.KeePassXC/config/keepassxc/`) is reported as a
missing or unverified setting.

### F7. Development tool versions exceed project pins (confirmed)

`~/.venvs/tools` on `beta` has Black 26.10.1 and Sphinx 9.2.0, while
`pyproject.toml` pins `black>=25,<26` and `sphinx>=8,<9`. Black 26 would
reformat 19 files at `db06c97`, so `black --check` fails on unchanged
sources; the documentation tests pass under Sphinx 9.2. Decide whether to
raise the pins (and reformat in a separate commit) or install pinned
versions in an isolated environment.

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
