"""Zenity, kdialog, and custom-command interaction backends."""

import json
import subprocess
from pathlib import Path
from typing import Iterable, Sequence

from kp_sync.log import logged
from kp_sync.ui.base import UI, UIUnavailable


class _TkReviewMixin:
    """Shared conflict review window for GUI backends."""

    def review(
        self, conflicts: list[dict[str, object]]
    ) -> dict[str, str] | None:
        """Show merge choices with secrets masked by default.

        Args:
            conflicts: Conflict IDs, labels, and side values.

        Returns:
            Mapping from conflict ID to side, or ``None`` if deferred.

        Raises:
            UIUnavailable: If tkinter or a display is unavailable.
        """
        try:
            import tkinter as tk
            from tkinter import ttk
        except ImportError as exc:
            raise UIUnavailable(
                "tkinter is unavailable for conflict review"
            ) from exc

        result: dict[str, str] | None = None
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            raise UIUnavailable(
                "display unavailable for conflict review"
            ) from exc
        root.title("KeePass sync conflicts")
        root.geometry("800x500")
        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)
        columns = ("label", "base", "hub", "other", "choice")
        tree = ttk.Treeview(frame, columns=columns, show="headings")
        for column, label in (
            ("label", "Field"),
            ("base", "Base"),
            ("hub", "Hub"),
            ("other", "Other"),
            ("choice", "Keep"),
        ):
            tree.heading(column, text=label)
            tree.column(column, width=150)
        tree.pack(fill="both", expand=True)
        choices: dict[str, str] = {}
        secret_values: dict[str, tuple[str, str, str]] = {}
        for conflict in conflicts:
            key = str(conflict["id"])
            secret = bool(conflict.get("secret"))
            values = [
                str(conflict.get(side, ""))
                for side in ("base", "hub", "other")
            ]
            if secret:
                secret_values[key] = tuple(values)
                values = ["[hidden]" if value else "" for value in values]
            choices[key] = "hub"
            tree.insert(
                "",
                "end",
                iid=key,
                values=(str(conflict["label"]), *values, choices[key]),
            )

        def cycle_choice(event: tk.Event) -> None:
            row = tree.identify_row(event.y)
            column = tree.identify_column(event.x)
            if row and column == "#5":
                choices[row] = {
                    "hub": "other",
                    "other": "base",
                    "base": "hub",
                }[choices[row]]
                values = list(tree.item(row, "values"))
                values[-1] = choices[row]
                tree.item(row, values=values)

        tree.bind("<Double-1>", cycle_choice)

        show_secrets = tk.BooleanVar(value=False)

        def toggle_secrets() -> None:
            for key, values in secret_values.items():
                visible = (
                    list(values)
                    if show_secrets.get()
                    else ["[hidden]" if value else "" for value in values]
                )
                row = list(tree.item(key, "values"))
                row[1:4] = visible
                tree.item(key, values=row)

        def finish() -> None:
            nonlocal result
            result = dict(choices)
            root.destroy()

        def defer() -> None:
            root.destroy()

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Label(
            buttons,
            text="Double-click the Keep column to cycle hub / other / base.",
        ).pack(side="left")
        ttk.Checkbutton(
            buttons,
            text="Show secrets",
            variable=show_secrets,
            command=toggle_secrets,
        ).pack(side="left", padx=8)
        ttk.Button(buttons, text="Apply choices", command=finish).pack(
            side="left", padx=8
        )
        ttk.Button(buttons, text="Defer", command=defer).pack(side="right")
        root.protocol("WM_DELETE_WINDOW", defer)
        root.after(self.timeout * 1000, defer)
        root.mainloop()
        return result


def _run_dialog(
    argv: list[str],
    *,
    timeout: int,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str] | None:
    # Dialog text can quote database content; a custom command gets
    # its request on stdin, so its own arguments are safe to show.
    dialog = Path(argv[0]).name in ("zenity", "kdialog")
    try:
        return logged(subprocess.run, hide_text=dialog)(
            argv,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    except OSError as exc:
        raise UIUnavailable(
            f"GUI dialog failed: {exc.__class__.__name__}"
        ) from exc


class ZenityUI(_TkReviewMixin, UI):
    """Desktop backend using Zenity and tkinter conflict review."""

    def __init__(self, *, timeout: int = 600) -> None:
        """Set the maximum duration for each dialog."""
        self.timeout = timeout

    def confirm(self, prompt: str, *, default: bool = False) -> bool | None:
        result = _run_dialog(
            ["zenity", "--question", "--text", prompt], timeout=self.timeout
        )
        if result is None:
            return None
        if result.returncode == 0:
            return True
        if result.returncode in {1, 5}:
            return False if result.returncode == 1 else None
        raise UIUnavailable(
            f"GUI dialog exited with status {result.returncode}"
        )

    def choose(self, prompt: str, options: Iterable[str]) -> str | None:
        result = _run_dialog(
            [
                "zenity",
                "--list",
                "--text",
                prompt,
                "--column",
                "Choice",
                *options,
            ],
            timeout=self.timeout,
        )
        if result is None or result.returncode in {1, 5}:
            return None
        if result.returncode != 0:
            raise UIUnavailable(
                f"GUI dialog exited with status {result.returncode}"
            )
        output = result.stdout.rstrip("\r\n")
        return output or None

    def password(self, prompt: str, *, slot: str | None = None) -> str | None:
        result = _run_dialog(
            ["zenity", "--password", "--text", prompt], timeout=self.timeout
        )
        if result is None or result.returncode in {1, 5}:
            return None
        if result.returncode != 0:
            raise UIUnavailable(
                f"GUI dialog exited with status {result.returncode}"
            )
        output = result.stdout.rstrip("\r\n")
        return output or None

    def file_path(self, prompt: str) -> Path | None:
        result = _run_dialog(
            ["zenity", "--file-selection", "--title", prompt],
            timeout=self.timeout,
        )
        if result is None or result.returncode in {1, 5}:
            return None
        if result.returncode != 0:
            raise UIUnavailable(
                f"GUI dialog exited with status {result.returncode}"
            )
        output = result.stdout.rstrip("\r\n")
        return Path(output) if output else None


class KDialogUI(_TkReviewMixin, UI):
    """Desktop backend using KDialog and tkinter conflict review."""

    def __init__(self, *, timeout: int = 600) -> None:
        """Set the maximum duration for each dialog."""
        self.timeout = timeout

    def confirm(self, prompt: str, *, default: bool = False) -> bool | None:
        result = _run_dialog(
            ["kdialog", "--title", "KeePass sync", "--yesno", prompt],
            timeout=self.timeout,
        )
        if result is None or result.returncode == 2:
            return None
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise UIUnavailable(
            f"GUI dialog exited with status {result.returncode}"
        )

    def choose(self, prompt: str, options: Iterable[str]) -> str | None:
        values = list(options)
        argv = ["kdialog", "--title", "KeePass sync", "--menu", prompt]
        for index, value in enumerate(values):
            argv.extend((str(index), value))
        result = _run_dialog(argv, timeout=self.timeout)
        if result is None or result.returncode == 1:
            return None
        if result.returncode != 0:
            raise UIUnavailable(
                f"GUI dialog exited with status {result.returncode}"
            )
        selected = result.stdout.rstrip("\r\n")
        try:
            return values[int(selected)]
        except (ValueError, IndexError) as exc:
            raise UIUnavailable(
                "kdialog returned an invalid menu choice"
            ) from exc

    def password(self, prompt: str, *, slot: str | None = None) -> str | None:
        result = _run_dialog(
            ["kdialog", "--title", "KeePass sync", "--password", prompt],
            timeout=self.timeout,
        )
        if result is None or result.returncode == 1:
            return None
        if result.returncode != 0:
            raise UIUnavailable(
                f"GUI dialog exited with status {result.returncode}"
            )
        output = result.stdout.rstrip("\r\n")
        return output or None

    def file_path(self, prompt: str) -> Path | None:
        result = _run_dialog(
            ["kdialog", "--title", prompt, "--getopenfilename"],
            timeout=self.timeout,
        )
        if result is None or result.returncode == 1:
            return None
        if result.returncode != 0:
            raise UIUnavailable(
                f"GUI dialog exited with status {result.returncode}"
            )
        output = result.stdout.rstrip("\r\n")
        return Path(output) if output else None


class CustomCommandUI(_TkReviewMixin, UI):
    """Desktop backend using the configured JSON command protocol.

    Receives one JSON request on stdin. Passwords return through stdout,
    never argv or the environment.
    """

    def __init__(self, command: Sequence[str], *, timeout: int = 600) -> None:
        """Create a backend from a nonempty argument vector.

        Args:
            command: Executable and fixed arguments; no shell is used.
            timeout: Maximum seconds to wait for each command.

        Raises:
            ValueError: If ``command`` or an argument is empty.
        """
        if not command or any(not part for part in command):
            raise ValueError(
                "custom GUI command must be a non-empty argument list"
            )
        self.command = list(command)
        self.timeout = timeout

    def confirm(self, prompt: str, *, default: bool = False) -> bool | None:
        value = self._request(
            "confirm",
            prompt=prompt,
            default=default,
            expected=(bool, type(None)),
        )
        if value is None or isinstance(value, bool):
            return value
        raise UIUnavailable(
            "custom GUI command returned an invalid confirmation"
        )

    def choose(self, prompt: str, options: Iterable[str]) -> str | None:
        values = list(options)
        result = self._request(
            "choose", prompt=prompt, options=values, expected=(str, type(None))
        )
        if result is None:
            return None
        if not isinstance(result, str) or result not in values:
            raise UIUnavailable(
                "custom GUI command returned an unknown choice"
            )
        return result

    def password(self, prompt: str, *, slot: str | None = None) -> str | None:
        value = self._request(
            "password", prompt=prompt, expected=(str, type(None))
        )
        if value is None:
            return None
        if not isinstance(value, str):
            raise UIUnavailable(
                "custom GUI command returned an invalid password"
            )
        return value or None

    def file_path(self, prompt: str) -> Path | None:
        value = self._request(
            "file_path", prompt=prompt, expected=(str, type(None))
        )
        if value is not None and not isinstance(value, str):
            raise UIUnavailable(
                "custom GUI command returned an invalid file path"
            )
        return Path(value) if value else None

    def _request(
        self, operation: str, *, expected: tuple[type, ...], **fields: object
    ) -> object:
        request = (
            json.dumps({"operation": operation, **fields}, ensure_ascii=True)
            + "\n"
        )
        result = _run_dialog(
            self.command, timeout=self.timeout, input_text=request
        )
        if result is None or result.returncode == 1:
            return None
        if result.returncode != 0:
            raise UIUnavailable(
                f"custom GUI command exited with status {result.returncode}"
            )
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise UIUnavailable(
                "custom GUI command returned invalid JSON"
            ) from exc
        if type(value) not in expected:
            raise UIUnavailable(
                "custom GUI command returned an invalid response type"
            )
        return value
