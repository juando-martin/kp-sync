import hashlib
import os
import struct
import tempfile
from pathlib import Path


def kdbx4_bytes(payload: bytes) -> bytes:
    signature_and_version = bytes.fromhex("03d9a29a67fb4bb5") + struct.pack(
        "<HH", 0, 4
    )
    header = (
        signature_and_version + bytes((0,)) + struct.pack("<I", 4) + b"\0" * 4
    )
    header_hash = hashlib.sha256(header).digest()
    header_hmac = b"\0" * 32
    data_block = b"\0" * 32 + struct.pack("<I", len(payload)) + payload
    final_block = b"\0" * 32 + struct.pack("<I", 0)
    return header + header_hash + header_hmac + data_block + final_block


def write_kdbx4(path: str | Path, payload: bytes) -> Path:
    target = Path(path)
    target.write_bytes(kdbx4_bytes(payload))
    return target


def private_tempdir() -> tempfile.TemporaryDirectory:
    """Create a 0700 temporary directory on runtime storage.

    Installation safety checks reject group- or world-writable
    ancestors, so neither ``/tmp`` nor an arbitrary checkout location
    is reliable. ``XDG_RUNTIME_DIR`` is private to the user; without it,
    fall back to the configured temporary directory.
    """
    base = os.environ.get("XDG_RUNTIME_DIR") or None
    return tempfile.TemporaryDirectory(dir=base, prefix="kp-sync-test-")


# Argon2 with 2 passes, 1 MiB and one lane: about 2 ms per open instead
# of about 0.4 s with pykeepass's defaults (64 MiB, 14 passes). Synced
# and merged copies keep the header's parameters, so whole engine runs
# stay fast. Key derivation cost is not what these tests check; the
# compatibility tests keep KeePassXC's own defaults.
FAST_ARGON2 = {"I": 2, "M": 1024 * 1024, "P": 1}


def create_database(
    filename, password=None, keyfile=None, transformed_key=None
):
    """``pykeepass.create_database`` with a light key derivation."""
    from pykeepass import create_database as create

    database = create(
        filename,
        password=password,
        keyfile=keyfile,
        transformed_key=transformed_key,
    )
    parameters = (
        database.kdbx.header.value.dynamic_header.kdf_parameters.data.dict
    )
    if all(name in parameters for name in FAST_ARGON2):
        for name, value in FAST_ARGON2.items():
            parameters[name].value = value
        database.save()
    return database
