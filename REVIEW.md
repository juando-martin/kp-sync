# Continuing review lessons

This is a compact checklist of lessons that remain useful during changes,
not a chronology or a fresh code-review report. Detailed findings, fixes,
benchmarks and past validation are preserved in Git history. Authoritative
rules live in the chapters indexed by DESIGN; current work is in PLAN.

## History and synchronization

- Identical bytes can be committed independently by different hosts.
  Reconcile their metadata deterministically and union ancestry.
- A recovery marker needs a retry/resolution path, not just a pending
  message. Partial promotion must not fabricate a shared committed version.
- A remote metadata cache is safe only after a fresh checksum/size match;
  timestamps alone do not establish content identity. Reducing cloud
  round trips must not remove pre-write checks or download verification.
- Session identity is disposable; a historical record is not proof that
  a staged copy still exists. Retirement needs shared markers and reliable
  cleanup evidence, not local deletion or absence-based inference.

## Lifecycle and concurrency

- Daemon-local serialization does not protect preparation in a separate
  hook process. Rapid reinsertion requires the shared keydisk lock and
  pending-up reservation through daemon acknowledgement.
- Releasing locks for user interaction requires observation revalidation.
  Cleanup must also account for in-flight work paused at a question.
- Own-write filtering must use committed hashes without swallowing late
  saves or explicit triggers. Session completion must not duplicate sync.
- Keep staged/recovery data after incomplete synchronization, cleanup or
  key revocation. Never clear pending state merely to unblock a test.

## Credentials and formats

- Validate new passwords before caching; keep profile and alternate-copy
  slots distinct. Browser passwords are operation-only.
- Encrypted protocol tests must follow the actual server format, not a
  fake peer repeating client assumptions. KeePassXC increments response
  nonces little-endian; response actions belong to the outer envelope.
  Exercise real optional-library helpers as well as injected fakes.
- Equivalent XML defaults materialized by KeePassXC are not edits.
  Normalize only proven equivalents; preserve genuine-change timestamp
  checks, custom data and settings. Merge CustomData per key.
- No-password KDBX validation cannot authenticate keyed block HMACs.
  Structural checks still reject truncation and trailing data.
- Structural KDBX fixtures are not databases PyKeePass can decrypt.
  Never reuse live staging for synthetic fixtures.

## Integration and validation

- Synthetic configuration alone does not isolate process environment,
  keyrings, desktop notifications or runtime paths. Patch/inject all of
  them before constructing controllers and clients.
- Real disk topology needs tree traversal and correct serial inheritance;
  do not infer identity from a mount path shared by several drives.
- New default application launches must name the configured hub;
  process discovery must not match arbitrary command-line arguments.
- Check external command exit status: quiet output is not success.
  A successful cached status is neither a fresh probe nor end-to-end
  acceptance. A pairing success is not a provider-driven merge test.
- Unexpected worker/shutdown errors need explicit diagnostics and safe
  retention, not silent thread exits or success-shaped cleanup.
- Mutation-test affected safety checks. Keep deployment authorization
  distinct from code/test completion.

## Remaining caveats

The latest graphical client's display environment wins for daemon dialogs;
concurrent graphical sessions are not independently routed.
Displaced-file recovery backups intentionally are not ordinary rotated
backups; they remain tied to recovery markers in runtime storage.
`HistoryMaxItems=0` and `HistoryMaxSize=0` mean no entry history, matching
KeePass semantics.

Remaining acceptance gaps belong in PLAN. Open code-review findings live in
[REVIEW_FINDINGS.md](REVIEW_FINDINGS.md) until fixed or rejected; keep this
file free of current defects and resolved historical findings.
