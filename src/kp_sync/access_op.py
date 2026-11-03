"""Bridge kp-access file-operation requests to the kp-sync daemon."""

import os
import sys
from pathlib import Path
from typing import TextIO

from kp_sync.access_ops import (
    AccessOperationError,
    access_paths,
    create_upload,
    discard_upload,
    validate_operation,
)
from kp_sync.daemon.client import Client
from kp_sync.log import info


def run_access_op(
    config,
    *,
    environ=None,
    stdin=None,
    stdout=None,
    client=None,
) -> int:
    """Validate one kp-access request, forward it, and stream GET data.

    kp-access environment variables describe the request. PUT is staged
    before the daemon request; GET goes to ``stdout`` after approval.

    Returns:
        Zero after the requested operation completes.

    Raises:
        AccessOperationError: If the request or staging path is invalid,
            or staged data cannot be read.
        ClientError: If the daemon rejects or fails the request.
    """
    env = os.environ if environ is None else environ
    input_stream = sys.stdin.buffer if stdin is None else stdin
    output_stream = sys.stdout.buffer if stdout is None else stdout
    operation = env.get("OPERATION", "")
    owner_database_path = env.get("DB_PATH", "")
    run_directory = (
        client.directory if client is not None else _run_directory(config)
    )
    staging, staged_database, configured_database_path = access_paths(
        config,
        run_directory,
    )
    if (
        not owner_database_path
        or not Path(owner_database_path).expanduser().is_absolute()
        or Path(owner_database_path).expanduser() != staged_database
    ):
        raise AccessOperationError(
            "DB_PATH does not match the configured access staging database"
        )
    file_path = env.get("FILE_PATH", "")
    destination = env.get("DESTINATION", "")
    peer_address = env.get("PEER_ADDRESS", "")
    identified_user = env.get("IDENTIFIED_USER", "")

    upload_id = None
    if operation in {"PUT", "DELETE", "MOVE"} and not identified_user:
        raise AccessOperationError(
            "file operation requires an authenticated user"
        )
    if operation in {"GET", "PUT", "DELETE", "MOVE"}:
        validate_operation(
            config, run_directory, operation, file_path, destination
        )

    fields: dict[str, str] = {
        "operation": operation,
        "db_path": configured_database_path,
    }
    if file_path:
        fields["file_path"] = file_path
    if destination:
        fields["destination"] = destination
    if peer_address:
        fields["peer_address"] = peer_address
    if identified_user:
        fields["identified_user"] = identified_user

    if operation == "PUT":
        payload = input_stream.read()
        if not isinstance(payload, bytes):
            raise AccessOperationError("PUT input must be binary")
        upload_id = create_upload(
            config,
            run_directory,
            payload,
        )
        fields["upload_id"] = upload_id
    try:
        requester = client or Client(config)
        requester.request(
            "access-op",
            wait=True,
            ui=config.ui["mode"],
            **fields,
        )
    except BaseException:
        if upload_id is not None:
            discard_upload(
                config,
                run_directory,
                upload_id,
            )
        raise

    if operation == "GET":
        path, _ = validate_operation(
            config,
            run_directory,
            "GET",
            file_path,
        )
        assert path is not None
        try:
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    output_stream.write(chunk)
        except OSError as exc:
            raise AccessOperationError(
                f"cannot read staged database: {exc}"
            ) from exc
    else:
        info(f"kp-access {operation} completed")
    return 0


def _run_directory(config):
    from kp_sync.paths import ensure_run_dir

    return ensure_run_dir(config.profile)


def main(config) -> int:
    """Run the access bridge using process environment and streams."""
    return run_access_op(config)
