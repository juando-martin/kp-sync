Reusing this documentation setup
================================

Use this guide and the following source files as a reference when adopting
the documentation setup in another Python repository. Adapt project-specific
content and imports rather than copying them unchanged.

Files to provide
----------------

* ``tools/docs.py``: strict HTML builds and loopback-only preview.
* ``docs/conf.py``: Sphinx extensions, metadata and source import paths.
* ``docs/_static/layout.css``: responsive columns, wrapping identifiers and
  independently scrolling desktop navigation.
* ``docs/*.rst``: page structure, authoring guidance and autodoc examples.
* ``docs/_ext/cli_help.py``: generated argparse command reference.
* ``docs/_ext/module_reference.py``: automatic remaining-module reference.
* ``tests/test_documentation.py``: build, preview and import-safety tests.
* ``pyproject.toml``, ``MANIFEST.in`` and ``.gitignore``: optional tooling
  dependencies, source-distribution inclusion and generated-file exclusions.

Instructions for the target project
-----------------------------------

Reuse this project's Sphinx documentation setup, adapting it to the target
repository rather than copying project-specific content.

* Use Sphinx classic with autodoc, Napoleon for Google-style PEP 257
  docstrings, and viewcode for source links.
* Preserve the responsive stylesheet, wrapping API identifiers and
  independently scrolling desktop sidebar. Narrow screens use normal
  stacked navigation.
* Provide ``python tools/docs.py build`` and
  ``python tools/docs.py serve --port 8000``. Builds must be fresh, nitpicky
  and fail on warnings. Preview must bind only to ``127.0.0.1`` and close
  cleanly on Ctrl-C. Preserve the ``--output PATH`` option.
* Generate API reference from source docstrings and CLI help from parser
  definitions without executing commands.
* Separate getting-started instructions, integration contracts, public API,
  internal reference, CLI reference and contributor instructions. Explicitly
  document which APIs are stable, provisional or internal.
* Document meaningful contracts, parameters, returns, errors, side effects
  and non-obvious safety constraints. Avoid boilerplate for trivial helpers.
* Documentation imports and builds must not load user configuration, start
  services, access credentials or contact external systems. After dependency
  installation, builds must work offline.
* Adapt tests to verify strict build flags, preview lifecycle and loopback
  binding, error propagation, output-path handling, import-safety boundaries,
  and stylesheet inclusion in generated HTML.
* Keep Sphinx optional (this setup uses ``sphinx>=8,<9``). Install the target
  project and its docs extra in an isolated virtualenv, not a production
  environment.
* Ignore generated HTML. Include documentation sources, extensions, CSS and
  tools in source distributions. Verify a standalone source distribution
  can build the documentation.
* Preserve runtime behavior and keep existing documentation consistent.

Required adaptations
--------------------

``docs/conf.py``
   Replace project metadata and source paths. This setup reads the version
   from ``pyproject.toml`` using stdlib ``tomllib`` and assumes Python 3.11
   or newer.

``docs/_ext/cli_help.py``
   Replace ``kp_sync.cli._build_parser``, the ``kp-sync`` command name,
   ``kp-sync-help`` directive and hidden ``service-run`` exclusion. Use a
   side-effect-free parser factory. The implementation uses argparse's
   private subparser attributes; retain generated-help tests when adapting
   it. Omit the extension if the target has no argparse CLI.

``docs/_ext/module_reference.py``
   Replace the ``src/kp_sync`` package path and ``kp-sync-modules`` directive.
   Discovery currently scans top-level RST files for explicit automodule
   entries, excludes ``__main__.py``, and avoids indexing package re-exports
   twice. Adapt discovery if the target uses multiple packages, another
   source layout or nested documentation pages.

``docs/*.rst`` and ``tests/test_documentation.py``
   Replace package names, API selections, commands and forbidden operations
   with those of the target project. The offline test must guard the target's
   actual configuration, credentials, lifecycle and external-I/O entry points.

``MANIFEST.in``
   Include the target's own reference files instead of kp-sync's design,
   plan and handoff files. Preserve inclusion of ``*.rst``, ``*.py`` and
   ``*.css`` under ``docs/``, tooling sources and exclusion of ``docs/_build``.

Validation
----------

From the target repository root, using an isolated environment::

   python3 -m venv /path/to/private-venv
   /path/to/private-venv/bin/python -m pip install -e '.[docs]'
   /path/to/private-venv/bin/python tools/docs.py build
   /path/to/private-venv/bin/python tools/docs.py serve --port 8000

Open ``http://127.0.0.1:8000`` and stop the preview with Ctrl-C.
Run the adapted documentation tests with the target project's test runner.
Check long API names and anchor navigation at desktop and mobile widths:
the index must not overlap content, desktop navigation must remain visible
when following anchors, and sidebar scrolling must not move the content.
The preview does not automatically rebuild after edits; rerun it to rebuild.
