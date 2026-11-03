"""Perform three-way KeePass merges and safe recovery comparisons."""

import base64
import copy
import io

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from lxml import etree
from pykeepass import PyKeePass
from pykeepass.exceptions import CredentialsError

from kp_sync.ui.base import UI
from kp_sync.paths import atomic_write

_ENTITY_IGNORED = {"History", "Times"}
_SPECIAL_KEYS = {"String", "Binary"}
_SECRET_KEYS = {"password", "otp", "totp", "recovery codes"}


class MergeError(RuntimeError):
    """Raised when a KeePass merge cannot be completed safely."""

    pass


class MergeCredentialError(MergeError):
    """Raised when available credentials cannot open a database."""

    pass


@dataclass(frozen=True)
class MergeOutcome:
    """Three-way merge result, conflicts, and chosen values."""

    merged: bool
    conflicts: tuple[dict[str, object], ...]
    unresolved: tuple[str, ...] = ()
    choices: dict[str, str] | None = None
    password: str | None = field(default=None, repr=False)
    key_file: Path | None = None


@dataclass(frozen=True)
class UnknownCopyOutcome:
    """Result of importing data from an unrecognized database copy."""

    candidate: Path | None
    conflicts: tuple[dict[str, object], ...]
    unresolved: tuple[str, ...] = ()
    choices: dict[str, str] | None = None
    password: str | None = field(default=None, repr=False)
    key_file: Path | None = None


@dataclass(frozen=True)
class ForeignCopyCheck:
    """Descent-check result and entities requiring timestamp review."""

    descends: bool
    suspicious: tuple[str, ...] = ()


def merge_databases(
    base_path: str | Path,
    hub_path: str | Path,
    other_path: str | Path,
    output_path: str | Path,
    *,
    password: str,
    key_file: str | Path | None = None,
    ui: UI | None = None,
    choices: dict[str, str] | None = None,
    credential_resolver: (
        Callable[[Path], tuple[str | None, Path | None]] | None
    ) = None,
    metadata_path: str | Path | None = None,
) -> MergeOutcome:
    """Merge a base, hub, and changed copy into ``output_path``.

    Inputs are opened with the supplied master key and are not modified.
    Conflicts are reviewed through ``ui`` or supplied by stable IDs in
    ``choices``. A resolver can provide per-input credentials. Output is
    written only when no conflicts remain.

    Returns:
        Merge status, conflict details, unresolved IDs, and choices.
    """
    base, _base_credentials = _open_database(
        Path(base_path), password, key_file, credential_resolver
    )
    hub, hub_credentials = _open_database(
        Path(hub_path), password, key_file, credential_resolver
    )
    other, other_credentials = _open_database(
        Path(other_path), password, key_file, credential_resolver
    )
    output_database, _ = _open_database(
        Path(metadata_path) if metadata_path is not None else Path(hub_path),
        password,
        key_file,
        credential_resolver,
    )
    _normalize_binaries(output_database, hub, base, other)

    cached_choices = dict(choices or {})
    root, conflicts, unresolved = _build_merge(
        base, hub, other, cached_choices
    )
    all_choices = dict(cached_choices)
    if unresolved and ui is not None:
        pending_conflicts = [
            item for item in conflicts if item["id"] in unresolved
        ]
        reviewed = ui.review(pending_conflicts)
        if reviewed is not None:
            valid_ids = set(unresolved)
            if any(
                key not in valid_ids or value not in {"hub", "other", "base"}
                for key, value in reviewed.items()
            ):
                raise MergeError("UI returned an invalid conflict choice")
            all_choices.update(reviewed)
            root, conflicts, unresolved = _build_merge(
                base, hub, other, all_choices
            )

    if unresolved:
        return MergeOutcome(
            False, tuple(conflicts), tuple(sorted(unresolved)), all_choices
        )

    key_source = _latest_master_key_database(hub, other)
    output_credentials = (
        hub_credentials if key_source is hub else other_credentials
    )
    master_key_changed = key_source.tree.findtext("./Meta/MasterKeyChanged")
    recycled = _recycle_deleted_entries(
        root, base, hub, other, output_database
    )
    output_database.password, output_database.keyfile = output_credentials
    _replace_root_group(output_database, root)
    _set_master_key_changed(output_database, master_key_changed)
    _merge_deleted_objects(output_database, base, hub, other)
    _record_deleted_entities(output_database, base, hub, other, root, recycled)
    _merge_custom_icons(output_database, base, hub, other)
    _trim_histories(root, output_database)
    _remove_kdbx3_header_hash(output_database)
    target = Path(output_path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        _save_database(output_database, target)
    except Exception as exc:
        raise MergeError(
            "could not write merged KeePass database: "
            f"{exc.__class__.__name__}"
        ) from exc
    return MergeOutcome(
        True,
        tuple(conflicts),
        choices=all_choices,
        password=output_credentials[0],
        key_file=output_credentials[1],
    )


def _open_database(
    path: Path,
    password: str,
    key_file: str | Path | None,
    resolver: Callable[[Path], tuple[str | None, Path | None]] | None,
) -> tuple[PyKeePass, tuple[str, Path | None]]:
    primary = (password, Path(key_file) if key_file is not None else None)
    try:
        return _try_open(path, *primary), primary
    except CredentialsError as exc:
        if resolver is None:
            raise MergeCredentialError(
                "database copy rejected the supplied credentials"
            ) from exc
    assert resolver is not None
    alternate_password, alternate_key_file = resolver(path)
    if alternate_password is None:
        raise MergeCredentialError("database copy needs separate credentials")
    alternate = (
        alternate_password,
        Path(alternate_key_file) if alternate_key_file is not None else None,
    )
    try:
        return _try_open(path, *alternate), alternate
    except CredentialsError as exc:
        raise MergeCredentialError(
            "database copy rejected the supplied credentials"
        ) from exc


def _try_open(path: Path, password: str, key_file: Path | None) -> PyKeePass:
    return PyKeePass(
        str(path),
        password=password,
        keyfile=str(key_file) if key_file is not None else None,
    )


def merge_unknown_copy(
    current_path: str | Path,
    incoming_path: str | Path,
    output_path: str | Path,
    *,
    password: str,
    key_file: str | Path | None = None,
    ui: UI | None = None,
    choices: dict[str, str] | None = None,
    credential_resolver: (
        Callable[[Path], tuple[str | None, Path | None]] | None
    ) = None,
    force_review: bool = False,
    restore_deleted: bool = False,
) -> UnknownCopyOutcome:
    """Import newer data from an unknown copy without old deletions.

    Differences are reviewed as two-way recovery choices. Unresolved
    conflicts leave ``output_path`` unwritten. ``force_review`` requires
    review; ``restore_deleted`` permits reintroducing deleted items.
    """
    current, current_credentials = _open_database(
        Path(current_path), password, key_file, credential_resolver
    )
    incoming, incoming_credentials = _open_database(
        Path(incoming_path), password, key_file, credential_resolver
    )
    _normalize_binaries(current, incoming)

    current_maps = _entity_maps(current)
    incoming_maps = _entity_maps(incoming)
    current_deleted = _deleted_uuid_set(current)
    eligible: list[tuple[str, etree._Element, etree._Element | None]] = []
    for identifier, entry in incoming_maps["entries"].items():
        old = current_maps["entries"].get(identifier)
        if old is None:
            if restore_deleted or identifier not in current_deleted:
                eligible.append((identifier, entry, None))
        elif force_review and (
            not _entity_equal(old, entry)
            or _parent_id(current_maps["entry_parents"].get(identifier))
            != _parent_id(incoming_maps["entry_parents"].get(identifier))
        ):
            eligible.append((identifier, entry, old))
        elif not force_review and _entity_change_time(
            entry
        ) > _entity_change_time(old):
            eligible.append((identifier, entry, old))

    conflicts: list[dict[str, object]] = []
    unresolved: set[str] = set()
    selected: list[etree._Element] = []
    resolved_choices = dict(choices or {})
    for identifier, entry, old in eligible:
        conflict_id = f"unknown-entry:{identifier}"
        conflicts.append(
            {
                "id": conflict_id,
                "label": f"Newer entry: {_entry_title(entry)}",
                "base": "(unknown)",
                "hub": _entry_summary(old) if old is not None else "(absent)",
                "other": _entry_summary(entry),
                "secret": _entry_has_secret(entry),
            }
        )
        choice = resolved_choices.get(conflict_id)
        if choice not in {"hub", "other", "base"}:
            unresolved.add(conflict_id)
        elif choice == "other":
            selected.append(entry)

    selected_groups: list[etree._Element] = []
    for identifier, group in incoming_maps["groups"].items():
        old = current_maps["groups"].get(identifier)
        if (
            old is None
            and identifier in current_deleted
            and not restore_deleted
        ):
            continue
        changed = (
            old is None
            or not _components_equal(old, group)
            or _parent_id(current_maps["group_parents"].get(identifier))
            != _parent_id(incoming_maps["group_parents"].get(identifier))
        )
        if not changed or (
            old is not None
            and not force_review
            and _entity_change_time(group) <= _entity_change_time(old)
        ):
            continue
        conflict_id = f"unknown-group:{identifier}"
        conflicts.append(
            {
                "id": conflict_id,
                "label": f"Group: {group.findtext('Name', '')}",
                "base": "(unknown)",
                "hub": (
                    old.findtext("Name", "") if old is not None else "(absent)"
                ),
                "other": group.findtext("Name", ""),
                "secret": False,
            }
        )
        choice = resolved_choices.get(conflict_id)
        if choice not in {"hub", "other", "base"}:
            unresolved.add(conflict_id)
        elif choice == "other":
            selected_groups.append(group)

    if unresolved and ui is not None:
        response = ui.review(
            [item for item in conflicts if item["id"] in unresolved]
        )
        if response is not None:
            if any(
                key not in unresolved or value not in {"hub", "other", "base"}
                for key, value in response.items()
            ):
                raise MergeError("UI returned an invalid unknown-copy choice")
            resolved_choices.update(response)
            return merge_unknown_copy(
                current_path,
                incoming_path,
                output_path,
                password=password,
                key_file=key_file,
                choices=resolved_choices,
                credential_resolver=credential_resolver,
                force_review=force_review,
                restore_deleted=restore_deleted,
            )
    if unresolved:
        return UnknownCopyOutcome(
            None,
            tuple(conflicts),
            tuple(sorted(unresolved)),
            resolved_choices,
            current_credentials[0],
            current_credentials[1],
        )
    if not selected and not selected_groups:
        return UnknownCopyOutcome(
            None,
            tuple(conflicts),
            choices=resolved_choices,
            password=current_credentials[0],
            key_file=current_credentials[1],
        )

    root = copy.deepcopy(current.tree.find("./Root/Group"))
    if root is None:
        raise MergeError("current database has no root group")
    output_entries = {
        _uuid(entry): entry
        for entry in root.xpath(".//Entry[not(ancestor::History)]")
    }
    output_groups = {_uuid(group): group for group in root.iter("Group")}
    output_groups[_uuid(root)] = root
    incoming_groups = incoming_maps["groups"]
    incoming_parents = incoming_maps["group_parents"]
    restored_ids: set[str] = set()

    def nearest(group_id: str | None) -> etree._Element:
        """Find the nearest surviving group for a recovered entry."""
        # Do not resurrect deleted groups. Move contents to a surviving
        # ancestor.
        seen = set()
        while (
            group_id and group_id not in output_groups and group_id not in seen
        ):
            seen.add(group_id)
            group_id = _parent_id(incoming_parents.get(group_id))
        return output_groups.get(group_id or "", root)

    for entity in (*selected_groups, *selected):
        group_id = _parent_id(entity.getparent())
        chain: list[etree._Element] = []
        while group_id and group_id not in output_groups:
            group = incoming_groups.get(group_id)
            if group is None:
                break
            if restore_deleted or group_id not in current_deleted:
                chain.append(group)
            group_id = _parent_id(incoming_parents.get(group_id))
        for group in reversed(chain):
            cloned = copy.deepcopy(group)
            _strip_children(cloned)
            parent = nearest(
                _parent_id(incoming_parents.get(_uuid(group) or ""))
            )
            if restore_deleted:
                _touch_entity(cloned, current)
                if (identifier := _uuid(group)) is not None:
                    restored_ids.add(identifier)
            parent.append(cloned)
            output_groups[_uuid(group)] = cloned

        if entity.tag == "Group":
            group = output_groups.get(_uuid(entity))
            if group is None:
                group = copy.deepcopy(entity)
                _strip_children(group)
                if restore_deleted:
                    _touch_entity(group, current)
                output_groups[_uuid(entity)] = group
            _replace_components(group, entity)
            if restore_deleted:
                _touch_entity(group, current)
                if (identifier := _uuid(group)) is not None:
                    restored_ids.add(identifier)
            parent = nearest(_parent_id(entity.getparent()))
            if parent is not None and group is not root:
                if parent is group or group in parent.iterancestors():
                    raise MergeError("unknown group move would create a cycle")
                parent.append(group)
            continue
        entry = entity
        identifier = _uuid(entry)
        if identifier is None:
            continue
        old = output_entries.get(identifier)
        cloned_entry = copy.deepcopy(entry)
        if restore_deleted:
            _touch_entity(cloned_entry, current)
            restored_ids.add(identifier)
        if old is not None:
            _merge_history(cloned_entry, old, old, entry)
            _remove_element(old)
        parent = nearest(_parent_id(entry.getparent()))
        parent.append(cloned_entry)
        output_entries[identifier] = cloned_entry

    _replace_root_group(current, root)
    if restore_deleted and restored_ids:
        deleted = current.tree.find("./Root/DeletedObjects")
        if deleted is not None:
            for record in tuple(deleted.findall("DeletedObject")):
                if record.findtext("UUID") in restored_ids:
                    deleted.remove(record)
    _merge_custom_icons(current, incoming)
    _trim_histories(root, current)
    if _latest_master_key_database(current, incoming) is incoming:
        current.password, current.keyfile = incoming_credentials
        _set_master_key_changed(
            current, incoming.tree.findtext("./Meta/MasterKeyChanged")
        )
        current_credentials = incoming_credentials
    _remove_kdbx3_header_hash(current)
    target = Path(output_path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _save_database(current, target)
    return UnknownCopyOutcome(
        target,
        tuple(conflicts),
        choices=resolved_choices,
        password=current_credentials[0],
        key_file=current_credentials[1],
    )


def merge_restore(
    current_path: str | Path,
    incoming_path: str | Path,
    output_path: str | Path,
    *,
    password: str,
    key_file: str | Path | None = None,
    ui: UI | None = None,
    choices: dict[str, str] | None = None,
    credential_resolver: (
        Callable[[Path], tuple[str | None, Path | None]] | None
    ) = None,
) -> UnknownCopyOutcome:
    """Merge selected historical data into the current database.

    Omitted entities are unchanged. Resolve conflicts through ``ui`` or
    ``choices``.
    """
    current, current_credentials = _open_database(
        Path(current_path),
        password,
        key_file,
        credential_resolver,
    )
    incoming, _ = _open_database(
        Path(incoming_path),
        password,
        key_file,
        credential_resolver,
    )
    _normalize_binaries(current, incoming)
    current_maps = _entity_maps(current)
    incoming_maps = _entity_maps(incoming)
    selected = dict(choices or {})
    conflicts: list[dict[str, object]] = []

    def offer(kind: str, identifier: str, old, source, *, existence=False):
        """Resolve one conflict or record it for later review."""
        if existence:
            key = f"restore:{kind}:{identifier}:existence"
            conflict = {
                "id": key,
                "label": f"Restore {kind}: "
                + (
                    _entry_title(source)
                    if kind == "entry"
                    else source.findtext("Name", "")
                ),
                "base": "(absent)",
                "hub": (
                    "(absent)"
                    if old is None
                    else (
                        _entry_summary(old)
                        if kind == "entry"
                        else old.findtext("Name", "")
                    )
                ),
                "other": (
                    _entry_summary(source)
                    if kind == "entry"
                    else source.findtext("Name", "")
                ),
                "secret": (
                    _entry_has_secret(source) if kind == "entry" else False
                ),
            }
            conflicts.append(conflict)
            return key

        old_parts = _components(old)
        source_parts = _components(source)
        for key in sorted(old_parts.keys() | source_parts.keys()):
            old_part, source_part = old_parts.get(key), source_parts.get(key)
            if source_part is None:
                continue
            if _canonical(old_part) == _canonical(source_part):
                continue
            conflict_id = f"restore:{kind}:{identifier}:{key}"
            conflicts.append(
                {
                    "id": conflict_id,
                    "label": f"Restore {kind} {identifier}: {key}",
                    "base": "(not applicable)",
                    "hub": _display(old_part, _canonical(old_part), key),
                    "other": _display(
                        source_part, _canonical(source_part), key
                    ),
                    "secret": _is_secret(key, old_part, source_part),
                }
            )
        old_parent = _parent_id(
            (
                current_maps["entry_parents"]
                if kind == "entry"
                else current_maps["group_parents"]
            ).get(identifier)
        )
        source_parent = _parent_id(
            (
                incoming_maps["entry_parents"]
                if kind == "entry"
                else incoming_maps["group_parents"]
            ).get(identifier)
        )
        if old_parent != source_parent:
            conflict_id = f"restore:{kind}:{identifier}:location"
            conflicts.append(
                {
                    "id": conflict_id,
                    "label": f"Restore {kind} {identifier}: location",
                    "base": "(not applicable)",
                    "hub": old_parent or "(root)",
                    "other": source_parent or "(root)",
                    "secret": False,
                }
            )
        return None

    for identifier, source in incoming_maps["groups"].items():
        old = current_maps["groups"].get(identifier)
        if old is None:
            offer("group", identifier, old, source, existence=True)
        else:
            offer("group", identifier, old, source)
    for identifier, source in incoming_maps["entries"].items():
        old = current_maps["entries"].get(identifier)
        if old is None:
            offer("entry", identifier, old, source, existence=True)
        else:
            offer("entry", identifier, old, source)

    unresolved = {
        str(item["id"])
        for item in conflicts
        if selected.get(str(item["id"])) not in {"hub", "other", "base"}
    }
    if unresolved and ui is not None:
        answer = ui.review(
            [item for item in conflicts if item["id"] in unresolved]
        )
        if answer is not None:
            if any(
                key not in unresolved or value not in {"hub", "other", "base"}
                for key, value in answer.items()
            ):
                raise MergeError("UI returned an invalid restore choice")
            selected.update(answer)
            unresolved = {
                str(item["id"])
                for item in conflicts
                if selected.get(str(item["id"]))
                not in {"hub", "other", "base"}
            }
    if unresolved:
        return UnknownCopyOutcome(
            None,
            tuple(conflicts),
            tuple(sorted(unresolved)),
            selected,
            current_credentials[0],
            current_credentials[1],
        )

    modified: set[str] = set()
    root = current.tree.find("./Root/Group")
    if root is None:
        raise MergeError("current database has no root group")
    current_maps = _entity_maps(current)
    incoming_maps = _entity_maps(incoming)
    current_entities = {
        "group": current_maps["groups"],
        "entry": current_maps["entries"],
    }
    incoming_entities = {
        "group": incoming_maps["groups"],
        "entry": incoming_maps["entries"],
    }
    for kind in ("group", "entry"):
        for identifier, source in incoming_entities[kind].items():
            old = current_entities[kind].get(identifier)
            existence_id = f"restore:{kind}:{identifier}:existence"
            if old is None:
                if selected.get(existence_id) != "other":
                    continue
                target = copy.deepcopy(source)
                if kind == "group":
                    _strip_children(target)
                _touch_entity(target, current)
                parent_id = _parent_id(source.getparent())
                parent = current_entities["group"].get(parent_id or "")
                (parent if parent is not None else root).append(target)
                modified.add(identifier)
                current_entities[kind][identifier] = target
                continue

            changed = False
            old_parts = _components(old)
            source_parts = _components(source)
            for key in old_parts.keys() | source_parts.keys():
                conflict_id = f"restore:{kind}:{identifier}:{key}"
                if selected.get(conflict_id) != "other":
                    continue
                _set_component(old, key, source_parts.get(key))
                changed = True
            location_id = f"restore:{kind}:{identifier}:location"
            if selected.get(location_id) == "other":
                parent_id = _parent_id(source.getparent())
                parent = current_entities["group"].get(parent_id or "")
                if parent is not None and old is not root:
                    if kind == "group" and (
                        parent is old or old in parent.iterancestors()
                    ):
                        raise MergeError(
                            "restore group move would create a cycle"
                        )
                    old_parent = old.getparent()
                    if old_parent is not None:
                        old_parent.remove(old)
                    parent.append(old)
                    changed = True
            if changed:
                _touch_entity(old, current)
                if kind == "entry":
                    _merge_history(old, old, old, source)
                modified.add(identifier)

    if not modified:
        return UnknownCopyOutcome(
            None,
            tuple(conflicts),
            choices=selected,
            password=current_credentials[0],
            key_file=current_credentials[1],
        )

    deleted = current.tree.find("./Root/DeletedObjects")
    if deleted is not None:
        for record in tuple(deleted.findall("DeletedObject")):
            if _uuid(record) in modified:
                deleted.remove(record)
    _merge_custom_icons(current, incoming)
    _remove_kdbx3_header_hash(current)
    target = Path(output_path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _save_database(current, target)
    return UnknownCopyOutcome(
        target,
        tuple(conflicts),
        choices=selected,
        password=current_credentials[0],
        key_file=current_credentials[1],
    )


def _set_component(
    entity: etree._Element,
    key: str,
    replacement: etree._Element | None,
) -> None:
    current = _components(entity).get(key)
    if current is not None:
        _remove_element(current)
    if replacement is None:
        return
    clone = copy.deepcopy(replacement)
    if key.startswith("CustomData:"):
        custom_data = entity.find("CustomData")
        if custom_data is None:
            custom_data = etree.SubElement(entity, "CustomData")
        custom_data.append(clone)
    elif key.startswith("Times:"):
        times = entity.find("Times")
        if times is None:
            times = etree.SubElement(entity, "Times")
        times.append(clone)
    else:
        entity.append(clone)


def check_foreign_descent(
    recorded_path: str | Path,
    incoming_path: str | Path,
    *,
    password: str,
    key_file: str | Path | None,
    recorded_at: str,
    credential_resolver: (
        Callable[[Path], tuple[str | None, Path | None]] | None
    ) = None,
) -> ForeignCopyCheck:
    """Check whether a foreign copy descends from its recorded base.

    Compare timestamps with ``recorded_at``. Return suspicious IDs for
    review instead of accepting implausible times silently.
    """
    recorded, _ = _open_database(
        Path(recorded_path), password, key_file, credential_resolver
    )
    incoming, _ = _open_database(
        Path(incoming_path), password, key_file, credential_resolver
    )

    before = _entity_maps(recorded)
    after = _entity_maps(incoming)
    deleted = _deleted_object_times(incoming)
    suspicious: list[str] = []
    descends = True
    recorded_ticks = int(_ticks_from_iso(recorded_at))
    now_ticks = _ticks_from_iso(datetime.now(timezone.utc).isoformat())
    for kind in ("entries", "groups"):
        for identifier, old in before[kind].items():
            new = after[kind].get(identifier)
            old_change = _entity_change_time(old)
            deletion = deleted.get(identifier)
            if new is None:
                if deletion is None or deletion <= old_change:
                    descends = False
                elif deletion > now_ticks or deletion < recorded_ticks:
                    suspicious.append(identifier)
                continue
            new_change = _entity_change_time(new)
            if new_change < old_change:
                descends = False
            changed = (
                not _foreign_entity_equal(old, new)
                or _parent_id(before["group_parents"].get(identifier))
                != _parent_id(after["group_parents"].get(identifier))
                if kind == "groups"
                else not _foreign_entity_equal(old, new)
            )
            if changed and (
                new_change > now_ticks or new_change < recorded_ticks
            ):
                suspicious.append(identifier)
        for identifier, new in after[kind].items():
            if identifier not in before[kind]:
                new_change = _entity_change_time(new)
                if new_change > now_ticks or new_change < recorded_ticks:
                    suspicious.append(identifier)
    return ForeignCopyCheck(descends, tuple(sorted(set(suspicious))))


def filter_foreign_stale_cache(
    recorded_path: str | Path,
    incoming_path: str | Path,
    output_path: str | Path,
    *,
    password: str,
    key_file: str | Path | None,
    recorded_at: str,
    credential_resolver: (
        Callable[[Path], tuple[str | None, Path | None]] | None
    ) = None,
    allow_clock_skew: bool = False,
) -> tuple[Path, tuple[str, ...]]:
    """Keep post-baseline changes from a foreign stale-cache save."""
    recorded, _ = _open_database(
        Path(recorded_path), password, key_file, credential_resolver
    )
    incoming, incoming_credentials = _open_database(
        Path(incoming_path), password, key_file, credential_resolver
    )
    _normalize_binaries(incoming, recorded)
    baseline = _entity_maps(recorded)
    writer = _entity_maps(incoming)
    baseline_deleted = _deleted_uuid_set(recorded)
    writer_deleted = _deleted_object_times(incoming)
    suspicious: list[str] = []
    now = _ticks_from_iso(datetime.now(timezone.utc).isoformat())
    recorded_ticks = int(_ticks_from_iso(recorded_at))
    baseline_times = {
        identifier: _entity_change_time(element)
        for kind in ("entries", "groups")
        for identifier, element in baseline[kind].items()
    }

    root = incoming.tree.find("./Root/Group")
    if root is None:
        raise MergeError("foreign copy has no root group")
    writer_groups = writer["groups"]
    missing_groups = [
        identifier
        for identifier, old in baseline["groups"].items()
        if identifier not in writer_groups
        and writer_deleted.get(identifier, 0.0) <= _entity_change_time(old)
    ]
    for identifier in sorted(
        missing_groups, key=lambda item: _group_depth(item, baseline)
    ):
        old = baseline["groups"][identifier]
        clone = copy.deepcopy(old)
        _strip_children(clone)
        parent_id = _parent_id(baseline["group_parents"].get(identifier))
        parent = writer_groups.get(parent_id)
        if parent is None:
            parent = root
        parent.append(clone)
        writer_groups[identifier] = clone
        writer["group_parents"][identifier] = baseline["group_parents"].get(
            identifier
        )

    for identifier, old in baseline["groups"].items():
        new = writer_groups.get(identifier)
        if new is None:
            continue
        old_change = _entity_change_time(old)
        new_change = _entity_change_time(new)
        moved = _parent_id(
            writer["group_parents"].get(identifier)
        ) != _parent_id(baseline["group_parents"].get(identifier))
        changed = not _foreign_entity_equal(old, new) or moved
        if changed and (new_change > now or new_change < recorded_ticks):
            suspicious.append(identifier)
        if changed and new_change <= old_change:
            _replace_components(new, old)
            parent = writer_groups.get(
                _parent_id(baseline["group_parents"].get(identifier))
            )
            if moved and parent is not None:
                parent.append(new)
    for identifier, old in baseline["entries"].items():
        new = writer["entries"].get(identifier)
        old_change = _entity_change_time(old)
        deletion_time = writer_deleted.get(identifier, 0.0)
        if new is None:
            if deletion_time > old_change:
                if deletion_time > now:
                    suspicious.append(identifier)
                continue
            parent_id = _parent_id(baseline["entry_parents"].get(identifier))
            parent_group = baseline["groups"].get(parent_id)
            if (
                parent_id is not None
                and parent_group is not None
                and writer_deleted.get(parent_id, 0.0)
                > _entity_change_time(parent_group)
            ):
                continue
            parent = writer_groups.get(parent_id)
            if parent is None:
                parent = root
            parent.append(copy.deepcopy(old))
            writer["entries"][identifier] = parent[-1]
            writer["entry_parents"][identifier] = parent
            continue
        new_change = _entity_change_time(new)
        content_changed = not _foreign_entity_equal(old, new)
        moved = _parent_id(
            writer["entry_parents"].get(identifier)
        ) != _parent_id(baseline["entry_parents"].get(identifier))
        if (content_changed or moved) and (
            new_change > now or new_change < recorded_ticks
        ):
            suspicious.append(identifier)
        if (content_changed or moved) and new_change <= old_change:
            parent = writer_groups.get(
                _parent_id(baseline["entry_parents"].get(identifier))
            )
            replacement = copy.deepcopy(old)
            if parent is not None:
                _remove_element(new)
                parent.append(replacement)
                writer["entries"][identifier] = replacement

    for identifier, new in tuple(writer["entries"].items()):
        if (
            identifier in baseline_deleted
            and identifier not in baseline["entries"]
        ):
            _remove_element(new)
    for identifier, group in tuple(writer_groups.items()):
        if (
            identifier in baseline_deleted
            and identifier not in baseline["groups"]
        ):
            _remove_element(group)
    for kind in ("entries", "groups"):
        for identifier, element in writer[kind].items():
            if identifier not in baseline[kind]:
                change_time = _entity_change_time(element)
                if change_time > now or change_time < recorded_ticks:
                    suspicious.append(identifier)

    if suspicious and not allow_clock_skew:
        return Path(output_path), tuple(sorted(set(suspicious)))

    deleted_root = incoming.tree.find("./Root/DeletedObjects")
    if deleted_root is not None:
        for record in tuple(deleted_root.findall("DeletedObject")):
            identifier = _uuid(record)
            if (
                identifier in baseline_times
                and _decode_kdbx_time(record.findtext("DeletionTime"))
                <= baseline_times[identifier]
            ):
                deleted_root.remove(record)
    _merge_deleted_objects(incoming, recorded)
    _remove_kdbx3_header_hash(incoming)
    output = Path(output_path)
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _save_database(incoming, output)
    return output, ()


def _build_merge(
    base: PyKeePass,
    hub: PyKeePass,
    other: PyKeePass,
    choices: dict[str, str],
) -> tuple[etree._Element, list[dict[str, object]], set[str]]:
    conflicts: list[dict[str, object]] = []
    unresolved: set[str] = set()
    bases = _entity_maps(base)
    hubs = _entity_maps(hub)
    others = _entity_maps(other)
    for identifier in bases["groups"]:
        choice = choices.get(f"group:{identifier}:existence")
        if choice not in {"hub", "other", "base"}:
            continue
        selected = {"hub": hubs, "other": others, "base": bases}[choice]
        group = selected["groups"].get(identifier)
        if group is None:
            continue
        for target in (hubs, others):
            if identifier in target["groups"]:
                continue
            for element in group.iter():
                entity_id = _uuid(element)
                if entity_id is None or element is group:
                    continue
                if element.tag == "Group":
                    target["groups"].setdefault(entity_id, element)
                    target["group_parents"].setdefault(
                        entity_id, element.getparent()
                    )
                elif element.tag == "Entry" and not any(
                    ancestor.tag == "History"
                    for ancestor in element.iterancestors()
                ):
                    target["entries"].setdefault(entity_id, element)
                    target["entry_parents"].setdefault(
                        entity_id, element.getparent()
                    )

    merged_groups: dict[str, etree._Element] = {}
    merged_parents: dict[str, str | None] = {}
    group_ids = (
        set(bases["groups"]) | set(hubs["groups"]) | set(others["groups"])
    )
    root_id = _uuid(hub.tree.find("./Root/Group"))
    for identifier in sorted(group_ids):
        template = hubs["groups"].get(identifier)
        if template is None:
            template = others["groups"].get(identifier)
        if identifier == root_id:
            template = hubs["groups"].get(identifier)
        merged = _merge_entity(
            "group",
            identifier,
            bases["groups"].get(identifier),
            hubs["groups"].get(identifier),
            others["groups"].get(identifier),
            conflicts,
            unresolved,
            choices,
            survivor_moved=any(
                _parent_id(maps["group_parents"].get(identifier))
                != _parent_id(bases["group_parents"].get(identifier))
                for maps in (hubs, others)
                if identifier in maps["groups"]
            ),
        )
        if merged is None:
            continue
        if template is None:
            continue
        _strip_children(merged)
        merged_groups[identifier] = merged
        parent_choice = _merge_component(
            "group",
            identifier,
            "location",
            _parent_id(bases["group_parents"].get(identifier)),
            _parent_id(hubs["group_parents"].get(identifier)),
            _parent_id(others["group_parents"].get(identifier)),
            conflicts,
            unresolved,
            choices,
        )
        if identifier in bases["groups"] and (
            identifier not in hubs["groups"]
            or identifier not in others["groups"]
        ):
            location_id = f"group:{identifier}:location"
            unresolved.discard(location_id)
            conflicts[:] = [
                item for item in conflicts if item["id"] != location_id
            ]
            parent_choice = choices.get(
                f"group:{identifier}:existence",
                "hub" if identifier in hubs["groups"] else "other",
            )
        merged_parents[identifier] = {
            "base": _parent_id(bases["group_parents"].get(identifier)),
            "hub": _parent_id(hubs["group_parents"].get(identifier)),
            "other": _parent_id(others["group_parents"].get(identifier)),
        }.get(parent_choice or "")

    merged_entries: dict[str, etree._Element] = {}
    entry_parents: dict[str, str | None] = {}
    entry_ids = (
        set(bases["entries"]) | set(hubs["entries"]) | set(others["entries"])
    )
    for identifier in sorted(entry_ids):
        merged = _merge_entity(
            "entry",
            identifier,
            bases["entries"].get(identifier),
            hubs["entries"].get(identifier),
            others["entries"].get(identifier),
            conflicts,
            unresolved,
            choices,
            survivor_moved=any(
                _parent_id(maps["entry_parents"].get(identifier))
                != _parent_id(bases["entry_parents"].get(identifier))
                for maps in (hubs, others)
                if identifier in maps["entries"]
            ),
        )
        if merged is None:
            continue
        parent_choice = _merge_component(
            "entry",
            identifier,
            "location",
            _parent_id(bases["entry_parents"].get(identifier)),
            _parent_id(hubs["entry_parents"].get(identifier)),
            _parent_id(others["entry_parents"].get(identifier)),
            conflicts,
            unresolved,
            choices,
        )
        if identifier in bases["entries"] and (
            identifier not in hubs["entries"]
            or identifier not in others["entries"]
        ):
            location_id = f"entry:{identifier}:location"
            unresolved.discard(location_id)
            conflicts[:] = [
                item for item in conflicts if item["id"] != location_id
            ]
            parent_choice = choices.get(
                f"entry:{identifier}:existence",
                "hub" if identifier in hubs["entries"] else "other",
            )
        merged_entries[identifier] = merged
        entry_parents[identifier] = {
            "base": _parent_id(bases["entry_parents"].get(identifier)),
            "hub": _parent_id(hubs["entry_parents"].get(identifier)),
            "other": _parent_id(others["entry_parents"].get(identifier)),
        }.get(parent_choice or "")

    if root_id not in merged_groups:
        raise MergeError("the root group could not be merged")
    for identifier in merged_groups:
        seen = {identifier}
        parent_id = merged_parents.get(identifier)
        while parent_id in merged_groups:
            if parent_id in seen:
                raise MergeError(
                    "conflicting group moves would create a cycle"
                )
            seen.add(parent_id)
            parent_id = merged_parents.get(parent_id)
    for identifier, element in merged_groups.items():
        if identifier == root_id:
            continue
        parent = merged_groups.get(merged_parents.get(identifier) or root_id)
        if parent is not None:
            parent.append(element)
    for identifier, element in merged_entries.items():
        parent = merged_groups.get(entry_parents.get(identifier) or root_id)
        if parent is not None:
            _merge_history(
                element,
                bases["entries"].get(identifier),
                hubs["entries"].get(identifier),
                others["entries"].get(identifier),
            )
            parent.append(element)
    return merged_groups[root_id], conflicts, unresolved


def _entity_maps(database: PyKeePass) -> dict[str, dict[str, Any]]:
    root = database.tree.find("./Root/Group")
    if root is None:
        raise MergeError("database has no root group")
    groups: dict[str, etree._Element] = {}
    group_parents: dict[str, etree._Element | None] = {}
    entries: dict[str, etree._Element] = {}
    entry_parents: dict[str, etree._Element] = {}
    for group in database.tree.xpath("./Root/Group | ./Root/Group//Group"):
        identifier = _uuid(group)
        if identifier is None:
            continue
        groups[identifier] = group
        parent = group.getparent()
        group_parents[identifier] = (
            parent if parent is not None and parent.tag == "Group" else None
        )
    for entry in database.tree.xpath(
        "./Root/Group//Entry[not(ancestor::History)]"
    ):
        identifier = _uuid(entry)
        if identifier is None:
            continue
        entries[identifier] = entry
        parent = entry.getparent()
        if parent is not None and parent.tag == "Group":
            entry_parents[identifier] = parent
    return {
        "groups": groups,
        "group_parents": group_parents,
        "entries": entries,
        "entry_parents": entry_parents,
    }


def _merge_entity(
    kind: str,
    identifier: str,
    base: etree._Element | None,
    hub: etree._Element | None,
    other: etree._Element | None,
    conflicts: list[dict[str, object]],
    unresolved: set[str],
    choices: dict[str, str],
    *,
    survivor_moved: bool = False,
) -> etree._Element | None:
    if base is None:
        if hub is None:
            return copy.deepcopy(other) if other is not None else None
        if other is None:
            return copy.deepcopy(hub)
    elif hub is None or other is None:
        if hub is None and other is None:
            return None
        survivor = other if hub is None else hub
        assert survivor is not None
        if _entity_equal(base, survivor) and not survivor_moved:
            return None
        hub_state = "deleted" if hub is None else "modified"
        other_state = "deleted" if other is None else "modified"
        choice = _merge_component(
            kind,
            identifier,
            "existence",
            "present",
            hub_state,
            other_state,
            conflicts,
            unresolved,
            choices,
            deleted_conflict=True,
        )
        selected = {"hub": hub, "other": other, "base": base}.get(choice or "")
        if selected is None:
            return None
        return copy.deepcopy(selected)

    assert hub is not None and other is not None
    template = copy.deepcopy(hub)
    for child in tuple(template):
        if child.tag not in _ENTITY_IGNORED and child.tag not in {
            "Group",
            "Entry",
        }:
            template.remove(child)
    components = {
        side: _components(element) if element is not None else {}
        for side, element in (("base", base), ("hub", hub), ("other", other))
    }
    keys = set().union(*(items.keys() for items in components.values()))
    merged: dict[str, etree._Element] = {}
    for key in sorted(keys):
        selected = _merge_component(
            kind,
            identifier,
            key,
            _canonical(components["base"].get(key)),
            _canonical(components["hub"].get(key)),
            _canonical(components["other"].get(key)),
            conflicts,
            unresolved,
            choices,
            base_element=components["base"].get(key),
            hub_element=components["hub"].get(key),
            other_element=components["other"].get(key),
        )
        if selected is None:
            continue
        selected_element = {
            "base": components["base"].get(key),
            "hub": components["hub"].get(key),
            "other": components["other"].get(key),
        }.get(selected)
        if selected_element is not None:
            merged[key] = copy.deepcopy(selected_element)
    times = _choose_times(base, hub, other)
    if times is not None:
        old_times = template.find("Times")
        if old_times is not None:
            template.remove(old_times)
        template.insert(0, times)
    ordered_keys = dict.fromkeys(
        key for side in ("hub", "other", "base") for key in components[side]
    )
    custom_data: etree._Element | None = None
    for key in ordered_keys:
        if key not in merged:
            continue
        if key.startswith("CustomData:"):
            if custom_data is None:
                custom_data = etree.SubElement(template, "CustomData")
            custom_data.append(merged[key])
        elif key.startswith("Times:"):
            if times is None:
                times = etree.SubElement(template, "Times")
            old = times.find(merged[key].tag)
            if old is not None:
                times.remove(old)
            times.append(merged[key])
        else:
            template.append(merged[key])

    history = _union_history(base, hub, other)
    for old_history in tuple(template.findall("History")):
        template.remove(old_history)
    if history is not None:
        template.append(history)
    return template


def _merge_component(
    kind: str,
    identifier: str,
    key: str,
    base_value: bytes | str | None,
    hub_value: bytes | str | None,
    other_value: bytes | str | None,
    conflicts: list[dict[str, object]],
    unresolved: set[str],
    choices: dict[str, str],
    *,
    base_element: etree._Element | None = None,
    hub_element: etree._Element | None = None,
    other_element: etree._Element | None = None,
    deleted_conflict: bool = False,
) -> str | None:
    if hub_value == other_value:
        return "hub" if hub_value is not None else None
    if hub_value == base_value:
        return "other" if other_value is not None else None
    if other_value == base_value:
        return "hub" if hub_value is not None else None

    conflict_id = f"{kind}:{identifier}:{key}"
    choice = choices.get(conflict_id)
    if choice not in {"hub", "other", "base"}:
        unresolved.add(conflict_id)
        choice = "hub"
    conflict: dict[str, object] = {
        "id": conflict_id,
        "label": f"{kind} {identifier}: {key}",
        "base": _display(base_element, base_value, key),
        "hub": _display(hub_element, hub_value, key),
        "other": _display(other_element, other_value, key),
        "secret": _is_secret(key, base_element, hub_element, other_element),
    }
    if deleted_conflict:
        conflict["label"] = f"{kind} {identifier}: deleted versus modified"
        conflict["base"] = "present"
        conflict["hub"] = "deleted" if hub_value == "deleted" else "present"
        conflict["other"] = (
            "deleted" if other_value == "deleted" else "present"
        )
    if conflict_id not in {str(item["id"]) for item in conflicts}:
        conflicts.append(conflict)
    selected = {"hub": hub_value, "other": other_value, "base": base_value}[
        choice
    ]
    return choice if selected is not None else None


def _components(element: etree._Element) -> dict[str, etree._Element]:
    components: dict[str, etree._Element] = {}
    for child in element.findall("Times/*"):
        if child.tag in {"Expires", "ExpiryTime"}:
            components[f"Times:{child.tag}"] = child
    for child in element:
        if child.tag in _ENTITY_IGNORED or child.tag in {"Group", "Entry"}:
            continue
        if not isinstance(child.tag, str):
            continue
        if child.tag == "CustomData":
            # Merge custom data per item to avoid key conflicts.
            for item in child.findall("Item"):
                components[
                    f"CustomData:{item.findtext('Key', default='')}"
                ] = item
            continue
        if child.tag in _SPECIAL_KEYS:
            field = child.findtext("Key", default="")
            key = f"{child.tag}:{field}"
        else:
            key = child.tag
        components[key] = child
    return components


def _components_equal(left: etree._Element, right: etree._Element) -> bool:
    left_components = _components(left)
    right_components = _components(right)
    return left_components.keys() == right_components.keys() and all(
        _canonical(left_components[key]) == _canonical(right_components[key])
        for key in left_components
    )


def _replace_components(
    target: etree._Element, source: etree._Element
) -> None:
    for child in tuple(target):
        if child.tag not in {"Group", "Entry"}:
            target.remove(child)
    for child in source:
        if child.tag not in {"Group", "Entry"}:
            target.append(copy.deepcopy(child))


def _group_depth(identifier: str, maps: dict[str, dict[str, Any]]) -> int:
    depth = 0
    seen = {identifier}
    parent = maps["group_parents"].get(identifier)
    while parent is not None:
        next_id = _uuid(parent)
        if next_id is None or next_id in seen:
            break
        seen.add(next_id)
        depth += 1
        parent = maps["group_parents"].get(next_id)
    return depth


def _remove_element(element: etree._Element) -> None:
    parent = element.getparent()
    if parent is not None:
        parent.remove(element)


def _foreign_entity_equal(left: etree._Element, right: etree._Element) -> bool:
    return _foreign_entity_content(left) == _foreign_entity_content(right)


def _foreign_entity_content(element: etree._Element) -> bytes:
    clone = copy.deepcopy(element)
    # KeePassXC writes defaults without changing modification time.
    defaults = (
        {
            "IconID": "48",
            "Notes": "",
            "IsExpanded": "true",
            "DefaultAutoTypeSequence": "",
            "EnableAutoType": "null",
            "EnableSearching": "null",
        }
        if clone.tag == "Group"
        else {
            "IconID": "0",
            "ForegroundColor": "",
            "BackgroundColor": "",
            "OverrideURL": "",
            "Tags": "",
        }
    )
    for child in tuple(clone):
        if (
            clone.tag == "Group"
            and child.tag
            in {"EnableAutoType", "EnableSearching", "IsExpanded"}
            and child.text is not None
            and child.text.casefold() in {"true", "false", "null"}
        ):
            child.text = child.text.casefold()
        if clone.tag == "Entry" and child.tag == "AutoType":
            for sequence in tuple(child.findall("DefaultSequence")):
                if (
                    not sequence.attrib
                    and len(sequence) == 0
                    and not sequence.text
                ):
                    child.remove(sequence)
        if child.tag in {"Group", "Entry"}:
            clone.remove(child)
        elif (
            child.tag in defaults
            and not child.attrib
            and len(child) == 0
            and (child.text or "") == defaults[child.tag]
        ):
            clone.remove(child)
        elif clone.tag == "Entry" and child.tag == "String":
            key, value = child.find("Key"), child.find("Value")
            if (
                not child.attrib
                and len(child) == 2
                and key is not None
                and not key.attrib
                and len(key) == 0
                and key.text
                in {"Title", "UserName", "Password", "URL", "Notes"}
                and value is not None
                and not value.attrib
                and len(value) == 0
                and not value.text
            ):
                clone.remove(child)
    return _subtree_canonical(clone)


def _entity_equal(left: etree._Element, right: etree._Element) -> bool:
    return _subtree_canonical(left) == _subtree_canonical(right)


def _subtree_canonical(element: etree._Element) -> bytes:
    clone = copy.deepcopy(element)
    for candidate in tuple(clone.iter()):
        candidate.tail = None
        if len(candidate) and candidate.text and not candidate.text.strip():
            candidate.text = None
        if candidate.tag == "History":
            parent = candidate.getparent()
            if parent is not None:
                parent.remove(candidate)
        elif candidate.tag == "Times":
            for child in tuple(candidate):
                if child.tag not in {"Expires", "ExpiryTime"}:
                    candidate.remove(child)
        elif candidate.tag in {
            "LastAccessTime",
            "UsageCount",
            "LocationChanged",
            "LastSelectedGroup",
            "LastTopVisibleEntry",
        }:
            parent = candidate.getparent()
            if parent is not None:
                parent.remove(candidate)
    for candidate in reversed(tuple(clone.iter())):
        candidate[:] = sorted(
            candidate,
            key=lambda item: (
                str(item.tag),
                item.findtext("Key", ""),
                item.findtext("UUID", ""),
                etree.tostring(item),
            ),
        )
    return etree.tostring(clone, method="c14n", with_comments=False)


def _canonical(element: etree._Element | None) -> bytes | None:
    if element is None:
        return None
    clone = copy.deepcopy(element)
    for child in clone.iter():
        child.tail = None
        if len(child) and child.text and not child.text.strip():
            child.text = None
    return etree.tostring(clone, method="c14n", with_comments=False)


def _display(
    element: etree._Element | None, value: bytes | str | None, key: str
) -> str:
    if element is not None:
        value_element = element.find("Value")
        if value_element is None and element.tag == "String":
            value_element = element.find("Value")
        if value_element is not None:
            return value_element.text or ""
        return "".join(element.itertext()) or "(empty)"
    if value == b"deleted":
        return "deleted"
    if value is None:
        return "(absent)"
    return str(value)


def _is_secret(key: str, *elements: etree._Element | None) -> bool:
    if key.rsplit(":", 1)[-1].casefold() in _SECRET_KEYS:
        return True
    return any(
        value is not None
        and (
            value.get("Protected", "").casefold() == "true"
            or (
                (child := value.find("Value")) is not None
                and child.get("Protected", "").casefold() == "true"
            )
        )
        for value in elements
    )


def _merge_history(
    merged: etree._Element,
    base: etree._Element | None,
    hub: etree._Element | None,
    other: etree._Element | None,
) -> None:
    history = _union_history(base, hub, other)
    if history is None:
        history = etree.Element("History")
    for source in (base, hub, other):
        if source is None or _entity_equal(merged, source):
            continue
        previous = copy.deepcopy(source)
        old_history = previous.find("History")
        if old_history is not None:
            previous.remove(old_history)
        if _subtree_canonical(previous) not in {
            _subtree_canonical(entry) for entry in history.findall("Entry")
        }:
            history.append(previous)
    if not history.findall("Entry"):
        return
    ordered = sorted(history.findall("Entry"), key=_entry_time)
    for child in tuple(history):
        history.remove(child)
    for child in ordered:
        history.append(child)
    for old in tuple(merged.findall("History")):
        merged.remove(old)
    merged.append(history)


def _union_history(*entities: etree._Element | None) -> etree._Element | None:
    entries: dict[bytes, etree._Element] = {}
    for entity in entities:
        history = entity.find("History") if entity is not None else None
        if history is None:
            continue
        for entry in history.findall("Entry"):
            canonical = _subtree_canonical(entry)
            entries[canonical] = copy.deepcopy(entry)
    if not entries:
        return None
    result = etree.Element("History")
    for entry in sorted(entries.values(), key=_entry_time):
        result.append(entry)
    return result


def _entry_time(element: etree._Element) -> float:
    return _decode_kdbx_time(element.findtext("Times/LastModificationTime"))


def _entity_change_time(element: etree._Element) -> float:
    return max(
        _decode_kdbx_time(element.findtext("Times/LastModificationTime")),
        _decode_kdbx_time(element.findtext("Times/LocationChanged")),
    )


def _touch_entity(element: etree._Element, database: PyKeePass) -> None:
    times = element.find("Times")
    if times is None:
        times = etree.SubElement(element, "Times")
    modified = times.find("LastModificationTime")
    if modified is None:
        modified = etree.SubElement(times, "LastModificationTime")
    modified.text = database._encode_time(datetime.now(timezone.utc))


def _decode_kdbx_time(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        decoded = base64.b64decode(value, validate=True)
        if len(decoded) == 8:
            return float(int.from_bytes(decoded, "little"))
    except (ValueError, TypeError):
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed - datetime(1, 1, 1, tzinfo=timezone.utc)).total_seconds()


def _deleted_object_times(database: PyKeePass) -> dict[str, float]:
    result: dict[str, float] = {}
    for record in database.tree.xpath("./Root/DeletedObjects/DeletedObject"):
        identifier = _uuid(record)
        deletion_time = _decode_kdbx_time(record.findtext("DeletionTime"))
        if identifier is not None:
            result[identifier] = max(
                result.get(identifier, 0.0), deletion_time
            )
    return result


def _ticks_from_iso(value: str) -> float:
    return _decode_kdbx_time(value)


def _choose_times(
    base: etree._Element | None,
    hub: etree._Element | None,
    other: etree._Element | None,
) -> etree._Element | None:
    times = [
        copy.deepcopy(element.find("Times"))
        for element in (hub, other, base)
        if element is not None and element.find("Times") is not None
    ]
    if not times:
        return None
    return max(
        times,
        key=lambda item: _decode_kdbx_time(
            item.findtext("LastModificationTime")
        ),
    )


def _replace_root_group(
    database: PyKeePass, merged_root: etree._Element
) -> None:
    container = database.tree.find("./Root")
    if container is None:
        raise MergeError("database has no root container")
    old_root = container.find("Group")
    if old_root is None:
        raise MergeError("database has no root group")
    container.replace(old_root, merged_root)


def _merge_deleted_objects(*databases: PyKeePass) -> None:
    root = databases[0].tree.getroot()
    target_container = root.find("Root")
    if target_container is None:
        return
    target = target_container.find("DeletedObjects")
    if target is None:
        target = etree.SubElement(target_container, "DeletedObjects")
    records: dict[str, etree._Element] = {}
    for database in databases:
        container = database.tree.find("Root")
        if container is None:
            continue
        deleted = container.find("DeletedObjects")
        if deleted is None:
            continue
        for record in deleted.findall("DeletedObject"):
            identifier = record.findtext("UUID")
            if identifier:
                existing = records.get(identifier)
                if existing is None or _decode_kdbx_time(
                    record.findtext("DeletionTime")
                ) > _decode_kdbx_time(existing.findtext("DeletionTime")):
                    records[identifier] = copy.deepcopy(record)
    for child in tuple(target):
        target.remove(child)
    for identifier in sorted(records):
        target.append(records[identifier])


def _merge_custom_icons(*databases: PyKeePass) -> None:
    target_meta = databases[0].tree.find("./Meta")
    if target_meta is None:
        return
    target = target_meta.find("CustomIcons")
    if target is None:
        target = etree.SubElement(target_meta, "CustomIcons")
    known = {icon.findtext("UUID") for icon in target.findall("Icon")}
    for database in databases[1:]:
        source = database.tree.find("./Meta/CustomIcons")
        if source is None:
            continue
        for icon in source.findall("Icon"):
            identifier = icon.findtext("UUID")
            if identifier and identifier not in known:
                target.append(copy.deepcopy(icon))
                known.add(identifier)


def _trim_histories(root: etree._Element, database: PyKeePass) -> None:
    maximum_items = _positive_int(
        database.tree.findtext("./Meta/HistoryMaxItems"), 10
    )
    maximum_size = _positive_int(
        database.tree.findtext("./Meta/HistoryMaxSize"), 6_291_456
    )
    for entry in root.iter("Entry"):
        history = entry.find("History")
        if history is None:
            continue
        versions = sorted(
            history.findall("Entry"), key=_entry_time, reverse=True
        )
        retained: list[etree._Element] = []
        total_size = 0
        for version in versions:
            size = len(etree.tostring(version, encoding="utf-8"))
            if (
                len(retained) >= maximum_items
                or total_size + size > maximum_size
            ):
                continue
            retained.append(version)
            total_size += size
        for version in tuple(history):
            history.remove(version)
        for version in reversed(retained):
            history.append(version)
        if not retained:
            entry.remove(history)


def _positive_int(value: str | None, default: int) -> int | float:
    try:
        parsed = int(value) if value is not None else default
    except ValueError:
        return default
    return parsed if parsed >= 0 else float("inf")


def _record_deleted_entities(
    output: PyKeePass,
    base: PyKeePass,
    hub: PyKeePass,
    other: PyKeePass,
    merged_root: etree._Element,
    recycled: set[str],
) -> None:
    previous = [_entity_maps(database) for database in (base, hub, other)]
    old_ids = {
        kind: set().union(*(mapping[kind] for mapping in previous))
        for kind in ("groups", "entries")
    }
    kept = {
        "groups": {_uuid(group) for group in merged_root.iter("Group")},
        "entries": {
            _uuid(entry)
            for entry in merged_root.xpath(".//Entry[not(ancestor::History)]")
        }
        - recycled,
    }
    source_elements: dict[str, etree._Element] = {}
    for mapping in previous:
        for kind in ("groups", "entries"):
            for identifier, element in mapping[kind].items():
                source_elements.setdefault(identifier, element)

    root = output.tree.find("./Root")
    if root is None:
        return
    deleted = root.find("DeletedObjects")
    if deleted is None:
        deleted = etree.SubElement(root, "DeletedObjects")
    known = {
        item.findtext("UUID") for item in deleted.findall("DeletedObject")
    }
    for kind in ("groups", "entries"):
        for identifier in sorted(old_ids[kind] - kept[kind]):
            element = source_elements.get(identifier)
            raw_uuid = (
                element.findtext("UUID") if element is not None else None
            )
            if not raw_uuid or raw_uuid in known:
                continue
            record = etree.SubElement(deleted, "DeletedObject")
            etree.SubElement(record, "UUID").text = raw_uuid
            etree.SubElement(record, "DeletionTime").text = (
                output._encode_time(datetime.now(timezone.utc))
            )
            known.add(raw_uuid)


def _normalize_binaries(target: PyKeePass, *sources: PyKeePass) -> None:
    pool = list(target.binaries)
    for database in (target, *sources):
        mapping: dict[str, str] = {}
        for index, data in enumerate(database.binaries):
            if data not in pool:
                target.add_binary(data)
                pool.append(data)
            mapping[str(index)] = str(pool.index(data))
        for value in database.tree.xpath("./Root//Entry/Binary/Value[@Ref]"):
            old_ref = value.get("Ref")
            if old_ref not in mapping:
                raise MergeError("attachment refers to missing binary data")
            value.set("Ref", mapping[old_ref])


def _save_database(database: PyKeePass, path: Path) -> None:
    payload = io.BytesIO()
    database.save(payload)
    atomic_write(path, payload.getvalue())


def _recycle_deleted_entries(
    root: etree._Element,
    base: PyKeePass,
    hub: PyKeePass,
    other: PyKeePass,
    metadata: PyKeePass,
) -> set[str]:
    if (
        metadata.tree.findtext("./Meta/RecycleBinEnabled", "").casefold()
        != "true"
    ):
        return set()
    kept = {
        _uuid(entry)
        for entry in root.xpath(".//Entry[not(ancestor::History)]")
    }
    sources = _entity_maps(base)["entries"]
    encoded_uuid = metadata.tree.findtext("./Meta/RecycleBinUUID")
    removed = {
        identifier
        for identifier in set(sources) - kept
        if not any(
            parent.tag == "Group" and parent.findtext("UUID") == encoded_uuid
            for parent in sources[identifier].iterancestors()
        )
    }
    if not removed:
        return set()
    recycle = next(
        (
            group
            for group in root.iter("Group")
            if encoded_uuid and group.findtext("UUID") == encoded_uuid
        ),
        None,
    )
    if recycle is None:
        from uuid import uuid4

        recycle = etree.SubElement(root, "Group")
        etree.SubElement(recycle, "UUID").text = (
            encoded_uuid
            if encoded_uuid and base64.b64decode(encoded_uuid) != bytes(16)
            else base64.b64encode(uuid4().bytes).decode("ascii")
        )
        etree.SubElement(recycle, "Name").text = "Recycle Bin"
        metadata.tree.find("./Meta/RecycleBinUUID").text = recycle.findtext(
            "UUID"
        )
    for identifier in sorted(removed):
        entry = copy.deepcopy(sources[identifier])
        _merge_history(
            entry,
            sources[identifier],
            _entity_maps(hub)["entries"].get(identifier),
            _entity_maps(other)["entries"].get(identifier),
        )
        recycle.append(entry)
    return removed


def _latest_master_key_database(hub: PyKeePass, other: PyKeePass) -> PyKeePass:
    hub_time = _parse_time(hub.tree.findtext("./Meta/MasterKeyChanged"))
    other_time = _parse_time(other.tree.findtext("./Meta/MasterKeyChanged"))
    return other if other_time > hub_time else hub


def _set_master_key_changed(
    output: PyKeePass, source_value: str | None
) -> None:
    output_meta = output.tree.find("./Meta")
    if output_meta is None or source_value is None:
        return
    target = output_meta.find("MasterKeyChanged")
    if target is None:
        target = etree.SubElement(output_meta, "MasterKeyChanged")
    target.text = source_value


def _parse_time(value: str | None) -> float:
    return _decode_kdbx_time(value)


def _remove_kdbx3_header_hash(database: PyKeePass) -> None:
    meta = database.tree.find("./Meta")
    if meta is not None:
        header_hash = meta.find("HeaderHash")
        if header_hash is not None:
            meta.remove(header_hash)


def _strip_children(group: etree._Element) -> None:
    for child in tuple(group):
        if child.tag in {"Group", "Entry"}:
            group.remove(child)


def _uuid(element: etree._Element | None) -> str | None:
    if element is None:
        return None
    value = element.findtext("UUID")
    if not value:
        return None
    try:
        return base64.b64decode(value, validate=True).hex()
    except (ValueError, TypeError):
        return value


def _parent_id(parent: etree._Element | None) -> str | None:
    return _uuid(parent)


def _deleted_uuid_set(database: PyKeePass) -> set[str]:
    return {
        decoded.hex()
        for element in database.tree.xpath(
            "./Root/DeletedObjects/DeletedObject/UUID"
        )
        if element.text
        for decoded in [base64.b64decode(element.text, validate=True)]
        if len(decoded) == 16
    }


def _entry_title(entry: etree._Element) -> str:
    return entry.findtext("String[Key='Title']/Value", default="(untitled)")


def _entry_summary(entry: etree._Element | None) -> str:
    if entry is None:
        return "(absent)"
    return "; ".join(
        (
            f"title={_summary_field(entry, 'Title')}",
            f"user={_summary_field(entry, 'UserName')}",
            f"url={_summary_field(entry, 'URL')}",
            "password=[hidden]",
        )
    )


def _summary_field(entry: etree._Element, name: str) -> str:
    return entry.findtext(f'String[Key="{name}"]/Value', default="")


def _entry_has_secret(entry: etree._Element) -> bool:
    return _is_secret(
        "Password",
        entry.find("String[Key='Password']"),
    )
