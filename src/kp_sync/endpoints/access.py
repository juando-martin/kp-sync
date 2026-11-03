"""File endpoint specialization for the kp-access staging copy."""

from pathlib import Path

from kp_sync.endpoints.file import FileEndpoint


class AccessEndpoint(FileEndpoint):
    """The database copy staged for the kp-access owner process."""

    def __init__(self, path: str | Path, identity: str = "session:kp-access"):
        """Bind the endpoint to the staged database path.

        Args:
            path: Local database path maintained for the access owner.
        """
        super().__init__(
            "kp-access",
            path,
            identity=identity,
            optional=True,
            on_unavailable="skip",
            foreign_writers=True,
        )
