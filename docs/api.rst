Python API reference
====================

Compatibility policy
--------------------

The qualified names explicitly listed in this page are the intended
integration surface, **provisional during 0.x**. Pin and test the kp-sync
version used by an integration. There is no claim of cross-version signature
or serialized-schema compatibility yet. Incompatible changes must be
documented before release; the daemon wire protocol separately checks its
own version.

Underscore-prefixed helpers, daemon controllers, XML merge internals,
serialized state files, and objects shown only in :doc:`internals` are not
public extension APIs. Documentation coverage does not change this policy.
Do not edit ``state.json`` or ``pending.json`` directly.

The internal history file currently uses schema version 2. Version-1 state
is migrated in memory and writes use version 2; unsupported versions are
rejected explicitly. This implementation detail is not a serialized-schema
compatibility promise. Clients sharing history must be upgraded before the
first retirement marker is published; older clients reject schema 2 rather
than rewriting it without retirement metadata.

Configuration
-------------

.. automodule:: kp_sync.config
   :members: Config, EndpointConfig, ConfigError, load_config

Engine
------

.. automodule:: kp_sync.engine
   :members: SyncEngine, SyncResult, SyncError, endpoints_from_config

The engine holds its profile flock during synchronization, releases it for
user questions, then revalidates before writing. It must not be used
concurrently through the same instance. Supply an explicit UI and credential
provider in integrations; defaults may prompt, access a keyring, or validate
the configured key file.

An explicit endpoint iterable is not merged automatically with configured
endpoints. Use ``endpoints_from_config`` for static endpoints; runtime drives,
sessions and kp-access staging have separate lifecycle adapters.
Custom endpoint injection does not register that transport as a baseline
store. History defaults to available supported stores or the runtime cache.

Results distinguish ``pending`` from ``missing``. ``consistent`` alone is
not proof that all configured endpoints were reached. A dry run still
probes and downloads; it is not an offline configuration check.
When no database copy can be used, ``action`` is ``unavailable`` and
``consistent`` is false, without creating a new pending decision. Sync
commands return exit status 4 for this case even with no configured
endpoints.

Endpoints
---------

.. automodule:: kp_sync.endpoints.base
   :members:

.. automodule:: kp_sync.endpoints.file
   :members: FileEndpoint

.. automodule:: kp_sync.endpoints.command
   :members: CommandEndpoint

.. automodule:: kp_sync.endpoints.rclone
   :members: RcloneEndpoint, EndpointMissing

Every physical copy needs an unambiguous, stable identity. A successful
fetch must return the actual bytes observed and a store must surface failures.
Implement probes conservatively: unknown checksums must not masquerade as
known bytes. A fingerprint's opaque ``token`` is not a content digest.
Set ``token_is_content`` only when the token changes whenever the content
changes (it covers a content digest of at least 64 bits); only then may an
unchanged token stand for a recorded version without a download.
``matches()`` may return ``None`` when it lacks a digest or a local path
for supported server-checksum verification; that is not proof of equality.
Cached observations must be invalidated at run and write
boundaries. Unsupported conditional promotion must fail safely; never
substitute check-then-overwrite for atomic exchange/noreplace.
File promotion protects atomic replace-style saves, not in-place writers.

Daemon client
-------------

.. automodule:: kp_sync.daemon.client
   :members: Client, LeaseConnection, ClientError, DaemonUnavailable

Client construction and requests use the process environment as well as the
configuration. Before constructing a synthetic client, patch
``XDG_RUNTIME_DIR`` to its private temporary runtime. ``activate=False`` on
requests is useful when activation must not occur. Normal requests can
start a daemon and contact real endpoints; do not use them during a docs
build.

UI and credentials
------------------

.. automodule:: kp_sync.ui.base
   :members:

.. automodule:: kp_sync.ui.none
   :members: NoneUI

.. automodule:: kp_sync.credentials
   :members: CredentialProvider, CredentialError

For unattended synthetic integrations use ``NoneUI`` and an injected fake
keyring, never the default real password cache. Preserve independent
alternate-copy password slots and the rule that secrets never enter
argv, the environment, logs or socket payloads.

The optional ``[credentials.keepassxc].url`` provider runs only when a
credential-requiring operation uses an interactive UI. It precedes the
manual cache; its validated password lives only for that operation, never
in the keyring. ``UI.credential_lookup`` wrappers release the engine flock
and daemon gate without serializing the callback or result over IPC.
Headless UIs skip it because browser entry approval may display dialogs.
Inject or mock ``kp_sync.keepassxc.retrieve`` for synthetic engine tests;
never pair a real database as part of tests or documentation builds.

The ``keepassxc`` extra is optional and loaded lazily. Pairing metadata is
private persistent state under ``$XDG_STATE_HOME/kp-sync/``, not runtime
database state. ``keepassxc-disconnect`` removes local pairing;
``forget`` clears manual password caches, not KeePassXC authorization.
