"""Compute KeePass hashes that ignore serialization-only changes."""

import hashlib
from pathlib import Path

from lxml import etree
from pykeepass import PyKeePass

_VOLATILE_TAGS = {
    "LastAccessTime",
    "UsageCount",
    "LocationChanged",
    "LastSelectedGroup",
    "LastTopVisibleEntry",
}


def content_hash(
    path: str | Path,
    *,
    password: str,
    key_file: str | Path | None = None,
) -> str:
    """Hash semantic database content while ignoring save-only metadata.

    pykeepass opens the database with supplied credentials. The digest
    identifies equivalent re-saves; it is not a KDBX integrity hash.
    """
    database = PyKeePass(
        str(path),
        password=password,
        keyfile=str(key_file) if key_file else None,
    )
    root = database.tree.getroot()
    for element in tuple(root.iter()):
        if not isinstance(element.tag, str):
            continue
        if element.tag in _VOLATILE_TAGS:
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
        elif (
            element.tag.endswith("Changed")
            and element.tag != "MasterKeyChanged"
            and _is_meta_timestamp(element)
        ):
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
    canonical = etree.tostring(root, method="c14n", with_comments=False)
    digest = hashlib.sha256(canonical)
    for binary in database.binaries:
        digest.update(len(binary).to_bytes(8, "little"))
        digest.update(binary)
    return digest.hexdigest()


def _is_meta_timestamp(element: etree._Element) -> bool:
    parent = element.getparent()
    return parent is not None and parent.tag == "Meta"
