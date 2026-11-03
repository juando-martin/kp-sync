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
