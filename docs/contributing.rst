Documentation and development
=============================

Install optional tools in a dedicated environment, not the shared production
virtualenv::

   python3 -m venv /path/to/private-venv
   /path/to/private-venv/bin/python -m pip install -e '.[docs,dev]'

Build HTML, or build and preview it locally::

   /path/to/private-venv/bin/python tools/docs.py build
   /path/to/private-venv/bin/python tools/docs.py serve --port 8000

The preview binds only to ``127.0.0.1``. Stop it with Ctrl-C.
``--output PATH`` selects another build directory; the default is ignored
``docs/_build/html``. Output is generated HTML, not database state.
The build is fresh, nitpicky, and treats warnings as errors. It requires no
network after dependencies are installed. Do not enable autodoc extensions
that construct engines, load user profiles, run examples, or open endpoints.

Authoring
---------

Use PEP 257 docstrings with Google-style Args, Returns, Raises and Notes
sections where needed; Napoleon renders them in Sphinx. Document module
responsibilities, class contracts, public operations, and non-obvious
internals, not trivial accessors or test functions. Explain blocking I/O,
locks, credentials, mutations and safe failure behavior for integrations.
An autodoc entry is not a promise of API stability.

Keep Python code at 79 columns, docstrings and comments at 72. Prefer
parenthesized continuation and adjacent string literals to backslashes.
The maintained scope is ``src/``, ``tests/``, ``examples/``, ``tools/``,
and Python documentation configuration. ``spikes/`` are historical throwaway
experiments, not maintained sources.

Run the configured format and style checks::

   python -m black --check src tests examples tools docs
   python -m pycodestyle --config=.pycodestyle src tests examples tools docs

Black's slice spacing and break-before-operator formatting are the only
documented checker exceptions (E203 and W503). Long strings must be split
without changing their values. Formatting must not alter runtime behavior.

Run the isolated unit suite with the package dependencies available::

   TMPDIR="$XDG_RUNTIME_DIR" PYTHONPATH=src python -m unittest discover -s tests

Tests use synthetic data, fake endpoints, and isolated runtime directories.
Optional local compatibility tools may cause skips. The real-cloud acceptance
harness is separate and must not run as part of documentation validation.
Never restart a production daemon merely to test documentation changes.

Keep README, man pages, examples, DESIGN, PLAN and the handoff consistent.
DESIGN is the decision record; PLAN records evidence and approval status.
Generate API reference from docstrings and command help from argparse.
Do not commit generated HTML or duplicate the authoritative design in prose.
