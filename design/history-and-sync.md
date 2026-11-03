# History, synchronization and restore

[Design index](../DESIGN.md)

## Baseline store

The history (`bases/<sha>.kdbx` and `state.json`) must survive reboots and
be shared by all machines, otherwise a sync cannot tell which side changed.
It is kept in a `kp-sync/` directory next to the database:

- **Primary: the cloud** (`REMOTE:kp-sync/`, via the `rclone` endpoint).
- **Offline replicas: pendrives** (`MOUNTPOINT/kp-sync/`). Every attached
  writable configured baseline drive retains reconciled history and all
  retained bases, even when the cloud is available. This lets a fresh
  runtime after reboot recognize the drive and fill its hub offline.

Rules:

- **Load** (start of `sync`): read `state.json` from every available store
  (the cloud and all attached drives) and from the runtime cache, and merge
  them: versions are a union (a version's file and hash never change once
  created; parents are unioned, see "Pruning"; two hosts committing the
  same bytes keep the earliest record's generation/time/host, and only
  differing sizes or content hashes are an error), and
  for each copy record keep the one with the highest record `generation`
  (taken from the history counter at the commit that wrote it; ties are
  broken by sync time, then host name, so all machines agree). Picking a
  whole file would be wrong: the cloud store may hold the newest record of
  the cloud while a pendrive holds the newest record of itself, written by
  another machine while offline. Then fetch the `bases/<sha>.kdbx` files
  needed for this run that are not already cached. A base is named by its
  SHA-256, so its integrity is checked on download. A `state.json` with an
  unknown schema version is not used (error, no guessing).
- **Save** (step 7 of the sync): upload new bases first, then `state.json`.
  Use the primary reachable cloud and every available writable configured
  pendrive store. Base files never
  change once written, so they are only ever added or deleted. `state.json`
  is replaced in place with `rclone copyto`. Before overwriting it, its
  `generation` is checked again; if another machine committed in between,
  the run restarts from step 2. rclone has no atomic compare-and-swap, so
  two machines committing within the same second can still race; the
  loser's commit is then detected by the next run as a cloud copy whose
  bytes do not match its record, and merged like any other change.
  An unchanged database does not advance copy-record generations on every
  poll: refresh observed records at most once a day (half `keep_days` when
  that is shorter; zero refreshes every run), or immediately when a copy's
  recorded version/bytes need updating. This retains liveness for pruning.
  A retirement-only update publishes `state.json` even when no database
  bytes or version changed; it does not create or upload a database base.
  Identical history is neither checked again nor uploaded nor followed by
  a base-directory pruning scan (no shared write happens). When something
  is published, one check precedes the `state.json` upload; its listing
  also names stale bases. Stale cloud bases are deleted in batches (at
  least eight, after one more state check), since a leftover
  content-addressed base only costs storage; drive stores prune at once.
  New or merged copy records and drive-to-cloud reconciliation still
  publish immediately. An empty or outdated drive store is brought current
  even when cloud history is unchanged; identical stores incur no writes.
  Bases are verified and copied before each target's state is published,
  with shared-state rechecks preserved. The first store in that order (the
  cloud, or the first drive when the cloud is unreachable) is required:
  its failure fails the run. Later drive stores are best-effort replicas
  once the required store holds the published state (published now or
  already current): a drive write, verification or pruning error is
  logged as `ERROR:`, recorded as a `failed:` publication outcome (or
  `published; pruning failed:` when its `state.json` was already
  replaced), and the run continues and finishes normally. A failed drive is
  left out of later checks in the same save and is retried by the next
  run, since its history still differs. A detected concurrent change
  (`BaselineChanged`) on any store still restarts the run.
  A drive left with outdated history is a user decision, asked after the
  run's local work (the hub and required store are committed; nothing is
  rolled back): "continue without updating it?" through the normal UI
  layer, outside the synchronization locks. Acceptance is kept in the
  private runtime file `replica-acknowledgements.json` and suppresses the
  question for that drive until its history is next published (or the
  runtime directory is lost at logout). A refusal, timeout or headless
  run (`none`, daemon polls) records a `drive-history:<identity>` pending
  decision, so the daemon notifies and `kp-sync resolve` asks again; the
  next run re-evaluates it and clears it when the drive publishes or is
  absent. The sync result stays consistent: this warning does not block
  launch, kp-access BEGIN or ejecting the drive. Drive replicas use more drive
  storage and local writes, not additional uploads to unchanged cloud
  history. Offline writes return to the cloud through normal reconciliation.
  Within a cloud store, previously downloaded `state.json` bytes (kept
  0600 in the profile run dir, so they survive daemon restarts) may be
  reused only after a fresh, duplicate-checked remote stat whose size and
  available supported checksums (SHA-256, SHA-1, MD5) match those bytes.
  Missing, changed or checksum-less metadata never authorizes reuse.
  This avoids redundant downloads, not the pre-write shared-state checks.
  Every rclone command logs its operation, endpoint name, elapsed time and
  success/failure, never its argument list, credentials or command output.
- **Reconcile**: when the cloud and a pendrive store are both available
  and the pendrive store has versions or records newer than the cloud's,
  the merged state and the bases it needs are uploaded to the cloud.
  All available drive stores retain the reconciled state and its bases;
  they are not removed after cloud publication. A vanished/unavailable
  drive is never treated as successfully updated. Existing drives without
  history need one successful sync with recognized history available before
  cold offline startup can rely on them. Unknown-copy safeguards remain
  unchanged; the launcher never bypasses them by opening a drive directly.
- **Retired disposable session copies (implemented).** A unique
  `session:...` identity represents a temporary
  staged copy, not a device expected to return. After access is revoked,
  final synchronization succeeds and staging is safely removed, retire
  its copy record instead of retaining it for `keep_days`. Keep the
  synchronized content in the version DAG subject to normal pruning; the
  retired session must no longer pin a version or merge base.
  Retirement is durable shared metadata (a tombstone keyed by the exact
  session identity), not merely deletion from the local `copies` map.
  Merge retirement markers across runtime/cloud/drive stores and let a
  marker suppress any copy record for that identity, including records
  from stale or offline stores. Session identities are never reused;
  retirement is terminal. Retain the compact markers independently of
  `keep_days`: time-based expiry alone cannot prevent resurrection from
  a long-offline store. Markers contain identity and ordering/audit
  metadata, not database bytes or credentials, and pin no versions.
  Introduce an explicit state-schema revision and reject unsupported
  schema versions; do not silently discard retirement metadata.
  Serialize retirement with normal history updates and retain all
  pre-publication concurrency checks.
  Persist cleanup progress privately in runtime storage before removing
  staging, so a crash between removal and retirement can finish the
  retirement on retry. Do not remove that progress until retirement is
  committed to runtime history; publish it through the normal baseline
  mechanism, including deferred publication when no store is reachable.
  Cleanup failure or incomplete final synchronization retains staging
  and its recovery record; never retire a potentially unsynchronized
  copy just because its owner exited or its path is missing.
  Legacy historical sessions without reliable successful-cleanup
  evidence must not be automatically retired based on absence alone,
  especially sessions owned by another host. Their migration needs
  explicit owner confirmation or reliable completion evidence.
  Provide a read-only legacy-retirement preview listing exact identities,
  eligibility evidence/reasons and blocked candidates before mutation.
  Applying the operation revalidates eligibility and concurrent state;
  preview alone never authorizes discarding unfinished data.
  Report retirement committed to runtime history separately from
  publication to each shared store. Explicitly identify deferred or failed
  publication; never imply that an unavailable store has received a marker.
  The current internal state schema is version 2. Version-1 state is read
  and migrated in memory; writes use version 2. Unknown versions fail
  explicitly. Clients that predate schema 2 reject it, so upgrade all
  clients that share history before publishing the first retirement marker.
  A retirement record is keyed by exact identity and stores its history
  generation, timestamp and host; it contains no database bytes and pins no
  version. The reusable legacy `session:kp-access` identity is never
  permanently retired by normal access cleanup: each new access lifecycle
  receives a unique `session:kp-access:<token>` identity. Its old fixed
  history record can be retired only through explicit exact-identity
  confirmation after runtime checks.
- **Pruning**: a version (and its base file) is kept while any copy record
  points to it, or while it is the merge-base of two such versions. When a
  version is pruned, its children take over its parents (the graph is
  contracted), so ancestry is never lost: a copy holding an old version is
  still recognised as `stale` even if every version in between was pruned.
  Copy records not updated for `keep_days` (default 180) are dropped,
  which lets their versions go; a pendrive unused for longer becomes `new`
  when it returns. Pruning deletes (cloud: in batches) from the store that was written and
  from the runtime cache. Since another store may still hold a pruned
  version and the old edges, the parents of a version are merged across
  stores as a union (which keeps ancestry correct), and a version brought
  back that way is pruned again by the next commit.
- **Neither store available** (no cloud, no pendrive): baselines stay in the
  runtime cache only, with a warning. They are saved to the first store that
  becomes available.
- **Store-name validation**: an explicitly configured `[baseline].stores`
  list must resolve to at least one enabled, writable baseline-capable
  endpoint (cloud or runtime-discovered pendrive), or configuration loading
  fails. Individual names that are missing, disabled, read-only or not
  baseline-capable produce warnings at load and in `doctor`; valid stores
  remain usable. The default list stays lenient for configurations that do
  not define those endpoints.
- The runtime cache retains a locally consistent commit before publishing it
  to a shared store, so an optimistic shared-state race and restart cannot
  lose that commit's ancestry. Shared snapshots are checked before database
  promotion as well as immediately before publishing history.
- **Sensitivity**: base files are encrypted KDBX files, no more sensitive
  than the database next to them. `state.json` holds only metadata (hashes,
  endpoint names, drive UUIDs, host names), never secrets or the key file.
- Copy records are keyed by the copy, not by the machine (`hub:HOST` for
  hubs), so several machines share one store and each sees what the others
  did to the cloud and to every pendrive.
- Phones and other Drive clients see the `kp-sync/` folder but ignore it.

## Explicit base cleanup

Explicit cleanup removes only recognized `<sha256>.kdbx` files absent from
the union of runtime and available shared version graphs. It deliberately
preserves every recorded version, including retained ancestors: it does not
expire copy records, contract the DAG, publish history or change retirement
markers. Normal synchronization owns history pruning. This conservative
policy protects disconnected copies and merge bases without downloading
base contents. Files with unrecognized names are left untouched.

Pending decisions, in-flight writes, unresolved recovery/retirement progress
or unavailable configured history block application. Preview remains useful
and identifies these blockers. Apply holds the access-operation lock followed
by the engine lock, rechecks local recovery/history and all shared snapshots
before each deletion, and aborts the affected scope on changed observations.
It bypasses automatic batching and works when history is unchanged.
Successful earlier deletions remain reported if a later deletion fails;
retries are safe. Runtime deletions do not follow symbolic links.

Shared deletion additionally requires explicit operator confirmation that
all other shared-history writers are paused for the entire operation.
This includes other hosts and profiles: the profile lock only serializes
local operations, and Google Drive does not provide atomic check-and-delete
against `state.json`. Fresh checks detect changes but cannot guarantee safety
if that operational precondition is violated. No age heuristic substitutes
for it. Unavailable history is not treated as an empty graph.

## Sync algorithm (`kp-sync sync`)

1. **Start**: the daemon runs one sync at a time and coalesces triggers
   (see "Daemon and leases"); a `--no-daemon` run takes `lock` instead,
   waiting up to `lock_timeout`. Nothing waits for the user while holding
   the sync slot (see "User interaction").
   Collect endpoints: the enabled configured ones, the attached drives in
   `drives/` (entries whose mount point is gone are removed), and live
   discovered sessions. A session whose owner process is gone (crashed
   client) is still synced once as a source, then closed as in "Remote
   hosts" (its ssh stopped, its `authorized_keys` line and files removed),
   so remote edits are not lost.
   With none, only the hub is checked. Load the state from the baseline
   stores and the runtime cache (merged per record, see "Baseline store").
2. **Probe**: for each endpoint, `probe` if supported; if the fingerprint
   equals the one recorded for a known version, its state follows without
   fetching.
   An `rclone` endpoint observes its database and the `kp-sync/` history
   with one filtered `lsjson --hash` listing (one rclone process; each costs
   1–2 s). A listed remote checksum (MD5 on Google Drive) that matches a
   local file already known to hold a version identifies the copy without
   downloading it, and a download is checked against the listed checksum:
   a mismatch means it changed in between and restarts the run.
   Otherwise fetch it into `tmp/`. Unreachable endpoints become `missing`,
   are reported according to `on_unavailable` and retried on the next run
   (no queue).
3. **Classify** the hub and every endpoint (see "Endpoint states"). A file
   whose bytes changed but whose content hash (computed when the password is
   cached: entries, groups, fields, attachments, deleted objects, ignoring
   access times, usage counts, `LocationChanged` and metadata timestamps
   whose value did not change) equals that of the version it diverged from is treated as
   holding that version; this avoids merges caused by plain re-saves, since
   KDBX is re-encrypted on every save.
   Copies with identical bytes are one source, not several. A `readonly`
   endpoint is never a source.
   - Nothing `changed`, `new` or `diverged` and nothing `stale`: done.
   - Nothing changed but some copies `stale`: promote the current version
     to them.
   - Exactly one `changed` source, and it diverged from the current
     version: **promote** it (after the "Foreign changes" check if the
     endpoint has `foreign_writers`).
   - A `changed` source that diverged from an older version (for example a
     pendrive edited on a machine that had not seen the latest version):
     **merge** with the current version, never promote.
   - Several changed with different contents: **merge**, then promote.
   - Any `diverged` copy (a known version on another branch): **merge**
     it with the current version, using their merge-base; no password is
     needed to detect it, only to merge (left pending without one).
   - Any `new` copy with differing bytes: see "Unknown copies" (step 6).
   - A `readonly` endpoint is never a target; a change found in one is kept
     as a side copy in `backups/` and reported once per distinct hash.
4. **Verify** a candidate before it is written anywhere:
   - always, without password: KDBX signature and version; for KDBX 4 the
     header SHA-256 and a walk of the HMAC block chain up to its final
     empty block, with no data after it (block lengths are in clear
     text, so truncation is detected without the key);
   - a candidate smaller than half of the version it diverged from (for
     example after removing large attachments) is not rejected but needs
     the full check below plus a confirmation, like an entry-count drop;
   - KDBX 3 has no such structure, so a KDBX 3 candidate always needs the
     full check below (password through the UI layer, or left pending);
   - when the password is available (cached, or a merge is running): it
     opens with the credentials, and its entry count is compared with
     the version it diverged from. A drop larger than both
     `max_entry_drop` and `min_entry_drop` is not a failure but a
     confirmation ("candidate has 40 fewer entries (-12 %), promote?");
     with no answer (no UI, *Later*, timeout) it is not promoted and the
     decision stays pending. A wrong confirmation can be undone from the
     backups, the history or Drive's version history. Unattended, the
     password is rarely cached, so this check mostly runs on merges and
     manual syncs;
   - a merge result (written by pykeepass) is also opened with
     `keepassxc-cli` (`db-info`), the reader that matters, judged by its
     exit code (`-q` hides the error message).
   A failed check aborts before any target is touched.
5. **Promote**: back up every target, then write the candidate to the hub
   and to every reachable endpoint. Before each write, re-check the target
   (`probe`, or hash of the file) against what was read in step 2; if it
   changed in the meantime, stop writing to it and restart the run from
   step 2 (at most `max_restarts` times, then report). These pre-write
   checks may reuse a cloud listing taken less than `RECHECK_WINDOW`
   (5 s, about one listing round trip) earlier; rclone has no
   compare-and-swap, so a fresher listing would not close the window. The
   post-write consistency checks always probe again. Writes are atomic
   where the endpoint allows it (rename for files, in-place `rclone copyto`
   for the cloud, staged copy for sessions).

   A pendrive copy that cannot be written (read-only remount, full exFAT,
   I/O error) is treated like an unreachable endpoint for this run, but
   only when a fresh check after the failure shows it still holds exactly
   the bytes read in step 2; a changed, missing or unreadable copy keeps
   failing the run, so displacement and crash-recovery rules are
   unaffected. The skipped drive keeps its copy record (it is `stale` and
   retried next run), is excluded from that run's commit set and from
   completing the in-flight candidate, and the run commits with every other
   copy consistent. After the run's local work it is a user decision like
   an outdated drive history (see "Baseline store"): accepted under the
   same acknowledgement file until the drive copy is next current, or a
   `drive-copy:<identity>` pending decision. Other endpoint types keep
   failing the run on a write error.

   A writable file endpoint is promoted with Linux
   `renameat2(RENAME_EXCHANGE)`: the staged candidate and target are
   atomically swapped, then the displaced target is hashed. If it matches
   the version read in step 2, the displaced file is removed. If it
   differs, it is retained as a timestamped runtime backup and the run
   becomes pending without committing history; an unresolved-race marker
   records the preserved backups. The next run resolves it: a backup whose
   SHA is already a known version is dropped; every other one is copied to
   `recovered/<sha>.kdbx` in the run dir and observed as an extra `new`
   (unknown) copy, so it is merged with the unknown-copy safeguards (newer
   entries reviewed, deletions not undone, nothing lost). It is never
   recorded as a copy. Once that run commits (or finds nothing to do), the
   marker and recovered files are removed and the SHAs listed in
   `resolved-displaced.json`; a displaced leftover on a drive with one of
   those SHAs is then removed instead of being preserved again. A backup
   that is not a KDBX file keeps the sync pending for manual resolution. A missing target is installed with
   `renameat2(RENAME_NOREPLACE)` so a file created concurrently is not
   overwritten.

   Filesystems without atomic exchange (exFAT, the usual pendrive
   filesystem, and vfat) use **displace-then-install** instead, built only
   from plain `rename` within one directory and `RENAME_NOREPLACE`, both of
   which exFAT supports:
   1. the candidate is staged in the target's directory (as for exchange)
      and fsynced;
   2. the target is renamed to a hidden displaced name in the same
      directory, `.<db_name>.kp-sync-displaced-<random>`, and the
      directory is fsynced;
   3. the staged candidate is installed with `RENAME_NOREPLACE` and the
      directory is fsynced. If a writer recreated the target in the gap,
      this fails with `EEXIST`: the staged candidate is removed, the
      writer's file is left in place, and the run restarts as for any
      changed target;
   4. the displaced file is hashed and handled exactly like the displaced
      file of an exchange: removed only if it matches the version read in
      step 2, otherwise kept as a timestamped runtime backup with the run
      left pending and the unresolved-race marker written.
   The trade-offs: for a few milliseconds the target path is absent (a
   reader opening it then fails and retries; on the main laptop
   applications open the hub, not the drive copy), and a crash or a
   pulled drive between steps 2 and 3 leaves no file at the target. The
   displaced name on the drive itself is the recovery record, so recovery
   does not depend on the runtime dir surviving a reboot. Before an
   endpoint is probed, any regular file in its directory matching that
   exact pattern (symlinks and other names are ignored) is resolved:
   - target absent: the displaced file is put back with
     `RENAME_NOREPLACE` (it is what the target held) and the run continues
     normally;
   - target present and the displaced file's hash equals the version
     recorded for that copy: it is the pre-promotion content, already in
     the history, and is removed;
   - otherwise both are kept: the displaced file is copied into
     `backups/` and the sync becomes pending, as for a detected race.
   Only `RENAME_EXCHANGE` may fall back, and only to this sequence. If
   `RENAME_NOREPLACE` is unsupported too, promotion fails safely. Never
   use a check-then-rename-over sequence, which could silently replace a
   concurrent save. exFAT has no journal, so an unclean removal can still
   corrupt the drive's filesystem; `eject` remains the clean way to
   remove a drive.

   A hub save after the promotion is caught by the normal post-write
   consistency check and restarts the run. Both mechanisms protect atomic
   replace-style saves by concurrent writers; writers that modify the
   target inode in place are not serialized by either.

   Before the first target write for a new candidate, save its verified
   content-addressed base in the runtime `bases/` and atomically record its
   hash, parent versions and planned writable copies in `inflight.json`.
   This is local provisional ancestry only: it does not update `state.json`
   or any shared baseline before all reachable copies are consistent. On a
   later run, an exact-hash copy matching the marker is classified through
   that provisional version; its base is verified and promotion can resume
   without an unknown-copy password prompt. A marker is discarded only when
   all its planned targets are observable and none holds that candidate, or
   after a consistent commit confirms the targets now hold the committed
   version. Keep the marker's bases and ancestry while any planned target is
   unreachable; defer history pruning during that time. A different machine
   that sees a partially written cloud copy has no access to this local
   marker and still applies the normal unknown-copy safeguards.
6. **Merge** (engine `kp-sync`): obtain the password (see Credentials). Start
   from the current version (or the hub, if it changed) and merge each
   other changed copy into it, 3-way, using the newest common ancestor of
   the two in the version history. Copies with identical
   bytes are merged once. Per entry and group (matched by UUID), and
   within an entry **per field**:
   - field changed on one side only: take that side (so a URL edit on one
     side and a password edit on the other is not a conflict);
   - field changed on both sides to the same value: take it;
   - field changed differently on both sides: conflict;
   - entry deleted on one side and unchanged on the other: delete (to the
     recycle bin if enabled, and recorded in deleted objects);
   - entry deleted on one side and modified on the other: conflict;
   - group deleted on one side while entries in it were added or modified
     on the other: conflict;
   - created on one side: add, recreating the group path;
   - moved to another group (including to the recycle bin): apply the move
     unless both moved it differently (conflict); an entry or group moved
     on one side and deleted on the other is an existence conflict.
   Compared fields: standard fields, custom properties (including
   protected ones), attachments, tags, expiry, icon, and entry/group
   `CustomData` per item key. Entry history: the
   union of both sides' history items (by modification time), plus the
   previous version of every entry changed by the merge, trimmed to the
   database's history limits. Deleted-object records: union. Database
   metadata (name, settings, KDF, cipher): the hub's; custom icons: union.
   Conflicts go to the review
   UI (field-by-field, secrets masked; `kdbx-merge-review` behaviour). With
   no usable UI (`--ui none`) or `review = "defer"`, the merge is not done:
   all copies are left as they are and the decision is recorded as
   pending (see "User interaction"). Then verify and promote.
   **Writing with pykeepass** (checked with pykeepass 4.2.0 on Python 3.10
   and 3.13, and KeePassXC 2.6.6, on KDBX 4.0 with and without a key
   file, the format of the real database): opening and
   saving a KeePassXC-written file loses nothing (tags, custom icons,
   `CustomData` of database, groups and entries, history, attachments,
   protected fields, auto-type, deleted-object records). Two gaps are
   handled in `kp_sync/merge.py` through pykeepass's lxml tree:
   - pykeepass has no API for deleted-object records and `delete_entry`
     records nothing, so the merge writes `DeletedObject` elements (UUID,
     deletion time) itself; KeePassXC keeps them;
   - on KDBX 3.1 pykeepass leaves a stale `Meta/HeaderHash`, and
     KeePassXC rejects every file it saves ("Header doesn't match hash");
     the element is removed before saving (it is optional), which fixes
     it.
   **Unknown copies** (`new`: a drive never seen, or seen before its
   version was pruned; also the very first run, with no history at all). If
   all copies are identical, record them as the first version. Otherwise
   the `new` copy is compared with the current version entry by entry
   (needs the password; left pending if not available):
   - it is **outdated** if every entry in it is equal to or older (by
     last-modification time) than the same entry in the current version,
     and every entry missing from the current version is listed in its
     deleted-object records. It is then overwritten with the current
     version (after a backup), like a `stale` copy;
   - otherwise only what is newer in it is merged: entries modified later
     than in the current version, and entries absent from both the current
     version and its deleted-object records. These go through the review
     UI (2-way, `kdbx-merge-review` semantics: nothing is deleted). Old
     deletions are therefore never undone by an old stick: a parent group
     listed in the current deleted-object records is not recreated, and
     an imported entry goes to its nearest surviving ancestor group.
   With no history at all and no current version, every differing entry is
   a conflict, as in `kdbx-merge-review`.
   **Foreign changes** (a `changed` copy on a `foreign_writers` endpoint,
   typically the cloud edited from a phone, or the hub saved by an
   application that had not reloaded an update; see "Applications").
   KDBX does not record which
   version a file was built from, and a phone app may save from an old
   offline cache. A plain 3-way merge against the recorded version would
   then read everything the cache lacks as deliberate edits: newer changes
   reverted and later entries deleted. So, with the password (left pending
   if not available; the copy is left untouched meanwhile):
   - **Descent check**: the copy descends from its recorded version if
     every entry and group of that version is in the copy with an equal or
     later last-modification time, or is listed in the copy's
     deleted-object records with a later deletion time. If so, it is
     handled normally (promote if it is the only change, else 3-way merge).
   - Otherwise it is a **stale-cache save**, and only what the writer
     really did is taken, using the recorded version as ancestor: entries
     and groups whose last-modification time in the copy is later than in
     the recorded version (their changed fields then follow the 3-way
     rules), entries absent from the recorded version and from its
     deleted-object records (created by the writer), and deletions whose
     deletion time is later than the entry's last modification in the
     recorded version. Anything the copy lacks or holds older is kept as
     in the current version. Conflicts go to review as usual; the user is
     notified that the cloud held a stale-cache save, and the result is
     promoted to every copy, the cloud included (after a backup).
   Last-modification times come from the writer's clock; a phone clock far
   off can misclassify, so entries whose times are in the future or older
   than the recorded version's own creation are reported in review rather
   than decided silently. Foreign descent and stale-cache comparisons treat
   absent standard fields and their equivalent empty/default values as
   equal: KeePassXC writes these defaults for untouched entries and groups
   without updating their modification times. This normalization is limited
   to known defaults (standard empty strings, entry colors/override/tags,
   default icons and group settings); custom fields, protection attributes
   and non-default values still count as changes. It affects comparisons,
   not the saved XML, and does not bypass timestamp review for real changes.
   Empty entry AutoType `DefaultSequence` is equivalent to its absence;
   boolean/null group settings compare case-insensitively.
   With `foreign_check = "merge-only"`, the check
   runs only when the copy has to be merged anyway.
   **Different master keys** (the password or key file was changed in one
   copy): a copy that does not open with the cached credentials asks for
   its own through the UI layer (left pending without a UI); the result
   takes the master key of the copy whose key changed most recently (KDBX
   metadata `MasterKeyChanged`), and the cache is updated to it. A
   one-sided promote needs no password, so a key change in one copy
   propagates like any other change. If optional verification encounters
   an obsolete cached credential, request the copy's own password and
   key-file choice (configured, another file, or none); without an answer,
   defer rather than treating the credential mismatch as corruption.
7. **Commit**: only when every reachable copy is consistent, add the new
   version to the history (parents: the versions it was built from), point
   every reachable copy's record at it, and prune. `missing` copies keep
   their old record. Save to the baseline store (cloud, otherwise the
   attached pendrives).

Nothing is silently overwritten: every overwrite has a backup, every write is
preceded by a verified candidate and a re-check of the target, and a failed
verification aborts before any target is touched.

## Restoring an old version

Restore first requires reachable source copies to have no unsynchronized
changed, diverged or unknown content. Otherwise it defers and asks for a
normal sync first, rather than building from a committed baseline and
overwriting uncommitted edits.

Putting an old file back by hand (for example with Drive's "restore
version") does not work: if its bytes are a known version it is `stale`
and the next sync overwrites it; if not, it is a foreign change from an
old cache and only entries modified after its recorded version are taken.
Restoring therefore goes through `kp-sync restore`, which tells the
history that the old content is wanted.

- **Sources**: `--version SHA` (a version still kept in the history; see
  `kp-sync history`), `--backup NAME` (a file in `backups/`, this boot
  only) or `--file PATH` (any KDBX, typically a revision downloaded by hand
  from Drive's version history, since rclone cannot list Drive revisions).
  Versions are pruned from the history (see "Baseline store"), so older
  points in time are usually recovered from Drive, which keeps revisions
  for a limited time unless marked "keep forever".
- **`--merge`** (default): a 2-way merge from the old copy into the current
  version in the conflict review window (`kdbx-merge-review` semantics,
  tkinter or terminal): entries missing from the current version
  (including deleted ones) and entries that differ are listed, and the
  user picks what to bring back, field by field. Nothing is removed from
  the current version. Recovered entries get a new modification time, so
  no copy treats them as old.
- **`--replace`**: the old content becomes the current version everywhere.
  The candidate is the old file with every entry and group it contains
  touched (new modification time) and the current version's later
  deletions dropped from its deleted-object records, so that no copy
  holding newer content wins a later merge by timestamps.
- Both need the password and always ask for confirmation, showing what
  changes (entries added, changed and removed; the large-drop check
  applies). The result is verified, promoted to every reachable copy (with
  backups) and committed as a new version whose parents are the current
  version and, when known, the restored one. It is therefore never seen as
  stale: copies that missed the restore are merged with it normally.
- `--dry-run` shows what would change without writing anything.
