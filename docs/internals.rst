Implementation reference
========================

These modules are documented for maintainers, not as stable integration
interfaces. DESIGN remains the authoritative architecture and safety record;
PLAN tracks acceptance evidence. The following module summaries come from
source docstrings. The view-source links expose method documentation without
promising internal helpers as public APIs.

Source decision records and manuals
-----------------------------------

* :download:`Design index and reading guide <../DESIGN.md>`
* :download:`Architecture and safety <../design/architecture.md>`
* :download:`History, synchronization and restore <../design/history-and-sync.md>`
* :download:`Daemon and runtime lifecycle <../design/lifecycle.md>`
* :download:`Endpoints and remote access <../design/endpoints-and-access.md>`
* :download:`Applications, interaction and credentials <../design/interaction.md>`
* :download:`Configuration and command contracts <../design/configuration-cli.md>`
* :download:`Development and implementation planning <../design/development.md>`
* :download:`Implementation plan and acceptance evidence <../PLAN.md>`
* :download:`README <../README.md>`
* :download:`Command man page <../man/man1/kp-sync.1>`
* :download:`Configuration man page <../man/man5/kp-sync-config.5>`

Module reference
----------------

.. automodule:: kp_sync.history
   :members:

.. automodule:: kp_sync.state
   :members:

.. automodule:: kp_sync.baseline
   :members:

.. automodule:: kp_sync.classify
   :members:

.. automodule:: kp_sync.merge
   :members:

.. automodule:: kp_sync.verify
   :members:

.. automodule:: kp_sync.access_ops
   :members:

.. automodule:: kp_sync.rclone_credentials
   :members:

.. automodule:: kp_sync.drive_sync
   :members:

.. automodule:: kp_sync.services
   :members:

.. automodule:: kp_sync.daemon.server
   :members:

.. automodule:: kp_sync.daemon.apps
   :members:

.. automodule:: kp_sync.sessions.session
   :members:

.. automodule:: kp_sync.ui.progress
   :members:
