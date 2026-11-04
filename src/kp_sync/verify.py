"""Verify candidate KDBX files before any copy is promoted."""

from dataclasses import dataclass
from pathlib import Path
import subprocess
import time

from kp_sync.kdbx import KdbxError, KdbxInfo, inspect_kdbx
from kp_sync.log import debug, logged
from kp_sync.ui.base import UI


@dataclass(frozen=True)
class Verification:
    """Structural verification details and any required confirmation."""

    info: KdbxInfo
    pending_reasons: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        """Whether all required checks are complete."""
        return not self.pending_reasons


class VerificationError(ValueError):
    """Raised when a candidate fails structural checks."""

    pass


class VerificationCredentialError(VerificationError):
    """Raised when a candidate or reference rejects its credentials."""

    def __init__(self, path: str | Path) -> None:
        super().__init__(
            "database could not be opened with the supplied credentials"
        )
        self.path = Path(path)


def verify_candidate(
    path: str | Path,
    *,
    reference_size: int | None = None,
    reference_path: str | Path | None = None,
    password: str | None = None,
    key_file: str | Path | None = None,
    reference_password: str | None = None,
    reference_key_file: str | Path | None = None,
    max_entry_drop: float = 0.10,
    min_entry_drop: int = 5,
    ui: UI | None = None,
    require_keepassxc: bool = False,
) -> Verification:
    """Verify a candidate structurally, using credentials when possible.

    Large size drops, KDBX 3 files, entry drops, and merges may need
    credentials or confirmation. Unconfirmed checks appear in
    ``pending_reasons``; malformed files raise ``VerificationError``.
    """
    try:
        info = inspect_kdbx(path)
    except (OSError, KdbxError) as exc:
        raise VerificationError(
            f"candidate failed KDBX verification: {exc}"
        ) from exc

    pending: list[str] = []
    size_drop = bool(reference_size and info.size * 2 < reference_size)
    needs_open = (
        info.major == 3
        or size_drop
        or reference_path is not None
        or require_keepassxc
    )
    if needs_open and password is None:
        if info.major == 3:
            pending.append("KDBX 3 requires password verification")
        if size_drop:
            pending.append(
                "candidate is smaller than half of its reference copy"
            )
        if require_keepassxc:
            pending.append("merged candidate requires KeePassXC verification")
        if reference_path is not None and info.major != 3 and not size_drop:
            pending.append("entry-count verification requires a password")
        return Verification(info, tuple(pending))

    if password is not None:
        try:
            from pykeepass import PyKeePass
            from pykeepass.exceptions import CredentialsError

            try:
                candidate = PyKeePass(
                    str(path),
                    password=password,
                    keyfile=str(key_file) if key_file is not None else None,
                )
            except CredentialsError as exc:
                raise VerificationCredentialError(path) from exc
            if reference_path is not None:
                reference_key = (
                    reference_key_file
                    if reference_password is not None
                    else key_file
                )
                try:
                    reference = PyKeePass(
                        str(reference_path),
                        password=(
                            reference_password
                            if reference_password is not None
                            else password
                        ),
                        keyfile=(
                            str(reference_key)
                            if reference_key is not None
                            else None
                        ),
                    )
                except CredentialsError as exc:
                    raise VerificationCredentialError(reference_path) from exc
                reference_count = len(reference.entries)
                candidate_count = len(candidate.entries)
                dropped = reference_count - candidate_count
                ratio = dropped / reference_count if reference_count else 0
                if dropped > min_entry_drop and ratio > max_entry_drop:
                    prompt = (
                        f"Candidate has {dropped} fewer entries "
                        f"(-{ratio:.0%}); promote?"
                    )
                    confirmed = ui.confirm(prompt) if ui is not None else None
                    if confirmed is not True:
                        pending.append(
                            "entry-count drop requires confirmation"
                        )
        except OSError as exc:
            raise VerificationError(
                f"candidate password verification failed: {exc}"
            ) from exc
        if size_drop and (
            ui is None
            or ui.confirm(
                (
                    "Candidate is smaller than half of its reference copy; "
                    "promote?"
                )
            )
            is not True
        ):
            pending.append("large size drop requires confirmation")
        if require_keepassxc:
            _verify_with_keepassxc(path, password, key_file)
    return Verification(info, tuple(pending))


def _verify_with_keepassxc(
    path: str | Path,
    password: str,
    key_file: str | Path | None,
) -> None:
    command = ["keepassxc-cli", "db-info", "--quiet"]
    if key_file is not None:
        command.extend(["--key-file", str(key_file)])
    command.append(str(path))
    started = time.monotonic()
    try:
        result = logged(subprocess.run)(
            command,
            input=password + "\n",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        debug(
            "keepassxc-cli db-info check: "
            f"{time.monotonic() - started:.3f}s ({type(exc).__name__})"
        )
        raise VerificationError(
            "KeePassXC compatibility verification failed: "
            f"{exc.__class__.__name__}"
        ) from exc
    debug(
        "keepassxc-cli db-info check: "
        f"{time.monotonic() - started:.3f}s (exit {result.returncode})"
    )
    if result.returncode != 0:
        raise VerificationError(
            f"KeePassXC rejected the candidate (exit {result.returncode})"
        )
