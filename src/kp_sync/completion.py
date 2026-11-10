"""Install Bash completion (``kp-sync completion install``).

Writes kp-sync's argcomplete registration and, for each alias such as
``kps`` (``alias kps='kp-sync --profile HOST'``), a file completing the
alias's expansion, into bash-completion's per-user directory, which it
loads on demand. Reads no configuration and contacts nothing.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from kp_sync.paths import atomic_write

_ALIAS = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")


def completion_dir(environ: Mapping[str, str] | None = None) -> Path:
    """bash-completion's per-user directory, found as it finds it."""
    env = os.environ if environ is None else environ
    if env.get("BASH_COMPLETION_USER_DIR"):
        base = Path(env["BASH_COMPLETION_USER_DIR"])
    else:
        home = Path(env.get("HOME", "~")).expanduser()
        data = env.get("XDG_DATA_HOME") or str(home / ".local" / "share")
        base = Path(data) / "bash-completion"
    return base / "completions"


def registration() -> str:
    """kp-sync's argcomplete registration for Bash."""
    import argcomplete

    return argcomplete.shellcode(["kp-sync"], shell="bash")


def alias_script(name: str) -> str:
    """Completion for alias ``name`` (see examples/bash-completion/kps).

    Raises:
        ValueError: If ``name`` is not a valid alias name.
    """
    if not _ALIAS.fullmatch(name) or name == "kp-sync":
        raise ValueError(f"invalid alias name: {name!r}")
    function = "_" + name.replace("-", "_") + "_complete"
    return (
        f"# Completion for the {name} alias (kp-sync --profile HOST): "
        "completes the\n"
        "# alias's expansion through kp-sync's argcomplete function.\n"
        'source "${BASH_SOURCE[0]%/*}/kp-sync"\n'
        "\n"
        f"{function}() {{\n"
        "    local prefix\n"
        f"    prefix=$(alias {name} 2>/dev/null) || return\n"
        f"    prefix=${{prefix#alias {name}=\\'}}\n"
        "    prefix=${prefix%\\'}\n"
        f'    local COMP_LINE="$prefix${{COMP_LINE#{name}}}"\n'
        "    local COMP_POINT=$((COMP_POINT + ${#prefix} - "
        f"{len(name)}))\n"
        "    _python_argcomplete kp-sync\n"
        "}\n"
        "complete -o nospace -o default -o bashdefault "
        f"-F {function} {name}\n"
    )


def install(
    *,
    aliases: Iterable[str] = (),
    dry_run: bool = False,
    environ: Mapping[str, str] | None = None,
    out: Callable[[str], None] = print,
) -> int:
    """Write the completion files; return the exit status.

    Raises:
        ValueError: If an alias name is invalid.
    """
    directory = completion_dir(environ)
    files = {"kp-sync": registration()}
    for name in aliases:
        files[name] = alias_script(name)
    for name, text in files.items():
        path = directory / name
        try:
            current = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = None
        if current == text:
            out(f"{path}: unchanged")
            continue
        new = current is None
        if dry_run:
            out(f"{path}: would {'install' if new else 'update'}")
            continue
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write(path, text)
        out(f"{path}: {'installed' if new else 'updated'}")
    if dry_run:
        out("Dry run: nothing was changed.")
    else:
        out(
            "New shells complete kp-sync"
            + "".join(f" and {name}" for name in aliases)
            + f"; in this one: source {directory / 'kp-sync'}"
        )
    for name in aliases:
        out(
            f"The alias itself belongs in ~/.bashrc, e.g. alias {name}="
            "'kp-sync --profile HOST'."
        )
    return 0
