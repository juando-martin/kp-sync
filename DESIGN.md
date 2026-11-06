# kp-sync design

This index and the chapters under `design/` together form the authoritative
architecture and safety design. Start here, then read the chapters relevant
to the task. Each decision has one authoritative home; other chapters refer
to it rather than duplicating its rules.

`PLAN.md` tracks current implementation and acceptance, and `NEXT_STEPS.md`
records the current handoff and deployment boundaries. Superseded work
descriptions remain in Git history; retain only lessons with continuing
value, not parallel narrative archives. Design text is not proof that
a feature is implemented: explicit planned decisions, including disposable
session retirement, remain pending until the implementation plan says
otherwise. Historical wording in the chapters is preserved by this
structure-only reorganization.

## Chapter index

| Chapter | Sections |
|---|---|
| [Architecture and safety](design/architecture.md) | [Goals](design/architecture.md#goals), [existing pieces](design/architecture.md#existing-pieces), [packaging decisions](design/architecture.md#packaging-decisions), [concepts](design/architecture.md#concepts), [error handling and safety](design/architecture.md#error-handling-and-safety), [feature parity](design/architecture.md#feature-parity-with-the-replaced-scripts) |
| [History, synchronization and restore](design/history-and-sync.md) | [Baseline store](design/history-and-sync.md#baseline-store), [sync algorithm](design/history-and-sync.md#sync-algorithm-kp-sync-sync), [restoring an old version](design/history-and-sync.md#restoring-an-old-version) |
| [Daemon and runtime lifecycle](design/lifecycle.md) | [Daemon and leases](design/lifecycle.md#daemon-and-leases), [runtime state](design/lifecycle.md#runtime-state-all-under-xdg_runtime_dirkp-sync) |
| [Endpoints and remote access](design/endpoints-and-access.md) | [Machines and pendrives](design/endpoints-and-access.md#machines-and-pendrives), [integration points](design/endpoints-and-access.md#integration-points) |
| [Applications, interaction and credentials](design/interaction.md) | [Applications](design/interaction.md#applications-keepassxc-and-others), [user interaction](design/interaction.md#user-interaction), [credentials](design/interaction.md#credentials) |
| [Configuration and command contracts](design/configuration-cli.md) | [Configuration](design/configuration-cli.md#configuration-toml), [commands](design/configuration-cli.md#commands) |
| [Development and implementation planning](design/development.md) | [Testing](design/development.md#testing), [documentation and integration API](design/development.md#documentation-and-integration-api), [implementation phases](design/development.md#implementation-phases), [open points](design/development.md#open-points) |

Section-name references in existing chapters and plans resolve through this
index, even when the referenced section now lives in another chapter.
Search all design text with `rg PATTERN DESIGN.md design/`.

## Safety-invariant reference

This is a navigation aid, not a second specification of the rules.

| Concern | Authoritative sections |
|---|---|
| Unknown/stale copies, foreign saves, verification, backups and atomic promotion | [Sync algorithm](design/history-and-sync.md#sync-algorithm-kp-sync-sync) |
| Shared history reconciliation, publication checks, pruning, session retirement and drive decommissioning (proposed) | [Baseline store](design/history-and-sync.md#baseline-store) |
| Lease ownership, synchronization serialization and runtime cleanup | [Daemon and leases](design/lifecycle.md#daemon-and-leases), [error handling and safety](design/architecture.md#error-handling-and-safety) |
| Private runtime storage and explicit persistence exceptions | [Runtime state](design/lifecycle.md#runtime-state-all-under-xdg_runtime_dirkp-sync), [baseline store](design/history-and-sync.md#baseline-store), [configuration](design/configuration-cli.md#configuration-toml), [credentials](design/interaction.md#credentials) |
| Credential lifetime, browser approval and secrets outside IPC/logs | [Credentials](design/interaction.md#credentials) |
| Safe deferral, pending decisions and question/progress lifecycle | [User interaction](design/interaction.md#user-interaction) |
| SSH confinement, revocation, staged-copy retention and access uploads | [Integration points](design/endpoints-and-access.md#integration-points) |
| Synthetic validation and safety mutations | [Testing](design/development.md#testing) |

## Task reading guide

| Task | Read together |
|---|---|
| Sync, classification, merge, restore or history changes | Architecture, history/sync, interaction, testing |
| Daemon scheduling, leases or cleanup | Lifecycle, safety, history/sync, interaction, testing |
| Disposable session retirement | Baseline store, lifecycle, integration points, command/status contracts, testing |
| Key drive decommissioning (proposed) | Baseline store, unknown copies, endpoints/access (udevmon actions), command/status contracts, testing |
| Endpoints, pendrives, SSH or kp-access | Endpoints/access, lifecycle, history/sync, configuration, testing |
| UI, applications or credentials | Interaction, lifecycle, configuration/commands, testing |
| Configuration or CLI changes | Configuration/commands and the chapter owning the affected behavior |
| Packaging, documentation or API work | Development, packaging decisions, affected contracts |

## Maintaining this design

- Read the applicable chapters before proposing or implementing changes.
- Put each new decision in its owning chapter and link to it elsewhere.
- Keep this index and contributor instructions consistent when moving or
  adding sections; remove resolved questions from Open points.
- Keep implementation progress and acceptance evidence in `PLAN.md` and
  `NEXT_STEPS.md`. Update those when implementing a planned decision.
- Structural moves should preserve section wording; substantive design
  revisions should be reviewed separately.

  Approved maintenance extensions (short status hashes, explicit base cleanup
  and daemon service controls) are specified in configuration/commands,
  history/sync and lifecycle. PLAN.md tracks their implementation.

Offline pendrive history is retained alongside cloud history; the baseline
store chapter owns publication and cold-start safeguards.

Symmetric daemon install/uninstall and activation enable/disable are specified
in the configuration/commands chapter, with lifecycle links.
