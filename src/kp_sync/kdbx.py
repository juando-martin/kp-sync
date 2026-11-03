"""Inspect KDBX structure and cleartext integrity metadata."""

import hashlib
import os
import struct
from dataclasses import dataclass
from pathlib import Path


SIGNATURE_1 = bytes.fromhex("03d9a29a")
SIGNATURE_2 = bytes.fromhex("67fb4bb5")


class KdbxError(ValueError):
    """Raised for invalid or unsupported KDBX structure."""

    pass


@dataclass(frozen=True)
class KdbxInfo:
    """Structural metadata returned by :func:`inspect_kdbx`."""

    major: int
    minor: int
    size: int
    sha256: str
    header_sha256: str | None
    block_count: int | None


def inspect_kdbx(path: str | os.PathLike[str]) -> KdbxInfo:
    """Verify signatures, header hashes, and the KDBX 4 block chain.

    This does not decrypt the database or verify credentials. Raises
    ``KdbxError`` for malformed content.
    """
    file_path = Path(path)
    size = file_path.stat().st_size
    with file_path.open("rb") as source:
        signature = source.read(12)
        if (
            len(signature) < 12
            or signature[:4] != SIGNATURE_1
            or signature[4:8] != SIGNATURE_2
        ):
            raise KdbxError("invalid KDBX signature")
        minor, major = struct.unpack("<HH", signature[8:12])
        if major not in {3, 4}:
            raise KdbxError(f"unsupported KDBX major version: {major}")

        digest = hashlib.sha256()
        source.seek(0)
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)

        if major == 3:
            return KdbxInfo(major, minor, size, digest.hexdigest(), None, None)

        position = 12
        source.seek(position)
        while True:
            field_header = source.read(5)
            if len(field_header) != 5:
                raise KdbxError("truncated KDBX 4 header field")
            field_id = field_header[0]
            length = struct.unpack_from("<I", field_header, 1)[0]
            position += 5
            if length > size - position:
                raise KdbxError("truncated KDBX 4 header value")
            source.seek(length, os.SEEK_CUR)
            position += length
            if field_id == 0:
                break

        if position + 64 > size:
            raise KdbxError("truncated KDBX 4 header hashes")
        source.seek(0)
        header_digest = hashlib.sha256()
        remaining = position
        while remaining:
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                raise KdbxError("truncated KDBX 4 header")
            header_digest.update(chunk)
            remaining -= len(chunk)
        calculated_header_hash = header_digest.digest()
        stored_header_hash = source.read(32)
        if calculated_header_hash != stored_header_hash:
            raise KdbxError("KDBX 4 header SHA-256 mismatch")
        source.seek(position + 64)
        position += 64

        blocks = 0
        while True:
            block_header = source.read(36)
            if len(block_header) != 36:
                raise KdbxError(
                    f"truncated KDBX 4 block header after {blocks} blocks"
                )
            block_size = struct.unpack_from("<I", block_header, 32)[0]
            position += 36
            if block_size > size - position:
                raise KdbxError(f"truncated KDBX 4 block {blocks}")
            source.seek(block_size, os.SEEK_CUR)
            position += block_size
            blocks += 1
            if block_size == 0:
                break
        if position != size:
            raise KdbxError("unexpected data after the final KDBX 4 block")

    return KdbxInfo(
        major,
        minor,
        size,
        digest.hexdigest(),
        calculated_header_hash.hex(),
        blocks,
    )


def sha256_file(path: str | os.PathLike[str]) -> tuple[str, int]:
    """Return SHA-256 and size for a structurally valid KDBX file."""
    info = inspect_kdbx(path)
    return info.sha256, info.size
