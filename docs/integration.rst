Integration contracts
=====================

Choosing an interface
---------------------

Prefer the CLI for scripts, hooks, application launchers and other processes.
It owns profile selection, daemon activation, UI routing and exit statuses.
Arguments are separate argv elements, never a shell command string.
``status`` is cached state, not evidence that an endpoint is reachable.
``--version`` is configuration-independent and performs no endpoint I/O.

Use the Python daemon client for structured requests from Python.
Use the in-process engine only when deliberately taking responsibility for
configuration, endpoint selection, UI, credentials, and runtime isolation.
The engine performs blocking work and can mutate copies even though it has
no daemon dependency.

Existing extension points
-------------------------

* **Command endpoint:** implement a bridge with fetch/store/probe argv arrays
  and ``{file}`` placeholders. It can integrate a new transport without
  introducing a Python plugin loader.
* **Python endpoint:** subclass :class:`kp_sync.endpoints.base.Endpoint` and
  pass an instance to :class:`kp_sync.engine.SyncEngine`. See :doc:`api` for
  fingerprint, identity, error and promotion obligations.
* **Custom GUI:** the JSON stdin/stdout question protocol is specified in
  DESIGN's User interaction section. Return the declared safe response on
  cancellation; never log protected conflict values.
* **udevmon:** actions run ``kp-sync --profile P drive-sync up|down
  --mode main|secondary``, which sends ``up/down`` on the main laptop and
  ``attach/detach`` elsewhere. Preserve physical drive identity and the
  shared keydisk pending-marker/lock invariant described in DESIGN.
* **rclone credentials:** a separate provider publishes the rclone config;
  kp-sync only reads it and keeps a private per-profile copy
  (:mod:`kp_sync.rclone_credentials`). Run the provider's ``down`` after
  kp-sync's ``down`` so the draining sync still has credentials.
* **kp-access:** stays a separate package. Use ``kp-sync access-op`` as its
  file-operation bridge; BEGIN/END own leases and staging safety. Do not
  bypass staging validation with direct database writes.

Process integration and credentials
-----------------------------------

Normal client requests use a private, same-uid Unix socket with a versioned,
bounded JSON-lines protocol. Use ``Client`` instead of generating raw wire
messages: validation and UI conversations are part of the protocol.
Passwords never cross that socket; the client caches them locally in the
profile-scoped kernel keyring and acknowledges that action.

Select the profile explicitly when automating a named configuration.
Runtime directories are per profile; keep owner profile and runtime aligned.
Do not confuse access virtual HTTP paths with local staging filenames.
Request failures must surface to the caller, not be converted to success.
Pending and missing outcomes need explicit handling.

No example in this manual uses real endpoints or credentials. Actual
integration tests must use an explicitly isolated synthetic profile and fake
keyring/UI, or separately approved test infrastructure.

Python client shape
-------------------

The following is an illustrative fragment, not a command executed by the
documentation build. ``config`` must already refer to the intended profile::

   from kp_sync.daemon.client import Client, LeaseConnection

   client = Client(config)
   cached = client.request("status", ui="none", activate=False)
   lease = LeaseConnection(client, "session:my-tool", kind="session")
   try:
       lease.ensure()
       # Use the integration while holding its connection-bound lease.
   finally:
       lease.close()

``activate=False`` refuses to start a missing daemon; it does not make an
arbitrary command read-only. ``status`` is cached, while a ``sync`` request
can fetch and write endpoints. Creating/renewing a lease can reconnect and
activate the daemon. Never put passwords in request fields.

The CLI equivalent for the cached query is ``kp-sync status --json``.
Use normal daemon commands rather than direct engine calls when managing
application, drive, access or session lifecycle.

Completeness assessment
-----------------------

The existing CLI, daemon client, injected endpoints and custom GUI already
cover the identified integration needs. No new runtime API is required for
this documentation phase.

Potential future additions, not implemented or promised here:

* A versioned endpoint factory/plugin registry for third-party TOML types.
  Today use command endpoints or explicit engine injection.
* A typed client response model with a public event/progress subscription.
  Today client replies are dictionaries; daemon events are internal.
* A separate baseline-store configuration adapter for custom transports.
  Implementing an endpoint alone does not make it baseline-capable.

Each needs a concrete caller, safety design and compatibility tests before
being accepted. Importing an internal helper is not a substitute for that
review.
