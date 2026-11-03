"""Offline Sphinx configuration; imports never activate a profile."""

from pathlib import Path
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "docs" / "_ext"))

project = "kp-sync"
release = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
    "version"
]
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "cli_help",
    "module_reference",
]
html_theme = "classic"
html_static_path = ["_static"]
html_css_files = ["layout.css"]
exclude_patterns = ["_build"]
autodoc_member_order = "bysource"
autodoc_typehints = "none"
napoleon_google_docstring = True
napoleon_numpy_docstring = False
