# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
import hashlib
import unittest
from datetime import datetime, timezone

from kp_sync.history import (
    CopyRecord,
    Version,
    current_version,
    is_ancestor,
    merge_base,
    prune_history,
)
from kp_sync.state import HistoryState, StateError, merge_states


def sha(number: int) -> str:
    return hashlib.sha256(str(number).encode()).hexdigest()


def version(
    number: int,
    parents=(),
    *,
    generation=1,
    host="host",
    time="2026-01-01T00:00:00+00:00",
):
    return Version(
        sha(number), tuple(parents), generation, host, time, size=100
    )


class HistoryTest(unittest.TestCase):
    def test_schema_one_migrates_and_retirement_dominates_stale_records(
        self,
    ) -> None:
        original = HistoryState()
        original.commit(
            sha(30), (), "host", ("session:remote:abcdef12",), size=100
        )
        legacy = original.to_dict()
        legacy["schema_version"] = 1
        legacy.pop("retirements")
        migrated = HistoryState.from_dict(legacy)
        self.assertEqual(migrated.copies, original.copies)
        self.assertEqual(migrated.to_dict()["schema_version"], 2)

        migrated.retire(
            "session:remote:abcdef12",
            "host",
            retired_at="2026-01-02T00:00:00+00:00",
        )
        stale = HistoryState(
            99,
            original.versions.copy(),
            {
                "session:remote:abcdef12": CopyRecord(
                    sha(30), 99, "2026-02-01T00:00:00+00:00", "stale"
                )
            },
        )
        for ordering in ((migrated, stale), (stale, migrated)):
            merged = merge_states(ordering)
            self.assertNotIn("session:remote:abcdef12", merged.copies)
            self.assertIn("session:remote:abcdef12", merged.retirements)
            with self.assertRaisesRegex(StateError, "retired session"):
                merged.record_copies(
                    sha(30), ["session:remote:abcdef12"], "host"
                )
            merged.prune(now=datetime(2026, 1, 3, tzinfo=timezone.utc))
            self.assertIn("session:remote:abcdef12", merged.retirements)
            self.assertEqual(merged.versions, {})

    def test_merge_base_heads_and_deterministic_current(self) -> None:
        root = version(1)
        left = version(2, (root.sha,), generation=2, host="a")
        right = version(3, (root.sha,), generation=2, host="b")
        merge = version(4, (left.sha, right.sha), generation=3)
        graph = {item.sha: item for item in (root, left, right, merge)}
        self.assertEqual(merge_base(left.sha, right.sha, graph), root.sha)
        self.assertTrue(is_ancestor(root.sha, merge.sha, graph))

        copies = {
            "left": CopyRecord(left.sha, 1, "2026-01-01T00:00:00+00:00", "a"),
            "right": CopyRecord(
                right.sha, 1, "2026-01-01T00:00:00+00:00", "b"
            ),
        }
        self.assertEqual(current_version(graph, copies), right.sha)
        copies["merged"] = CopyRecord(
            merge.sha, 1, "2026-01-01T00:00:00+00:00", "c"
        )
        self.assertEqual(current_version(graph, copies), merge.sha)

    def test_pruning_contracts_graph_and_keeps_merge_base(self) -> None:
        root = version(1)
        ancestor = version(2, (root.sha,), generation=2)
        left = version(3, (ancestor.sha,), generation=3, host="a")
        right = version(4, (ancestor.sha,), generation=3, host="b")
        graph = {item.sha: item for item in (root, ancestor, left, right)}
        recent = "2026-01-01T00:00:00+00:00"
        copies = {
            "left": CopyRecord(left.sha, 3, recent, "a"),
            "right": CopyRecord(right.sha, 4, recent, "b"),
            "expired": CopyRecord(
                root.sha, 1, "2020-01-01T00:00:00+00:00", "old"
            ),
        }
        pruned, records = prune_history(
            graph,
            copies,
            now=datetime(2026, 1, 2, tzinfo=timezone.utc),
            keep_days=180,
        )
        self.assertNotIn(root.sha, pruned)
        self.assertIn(ancestor.sha, pruned)
        self.assertEqual(merge_base(left.sha, right.sha, pruned), ancestor.sha)
        self.assertNotIn("expired", records)
        self.assertTrue(is_ancestor(ancestor.sha, left.sha, pruned))

    def test_state_merge_unions_version_parents_and_picks_newest_record(
        self,
    ) -> None:
        root = version(10)
        left = version(11, (root.sha,), generation=2, host="a")
        right = version(12, (root.sha,), generation=2, host="b")
        shared = version(13, (left.sha,), generation=3)
        expanded = version(13, (right.sha,), generation=3)
        a = HistoryState(
            5,
            {root.sha: root, left.sha: left, shared.sha: shared},
            {
                "cloud": CopyRecord(
                    shared.sha, 5, "2026-01-01T00:00:00+00:00", "a"
                )
            },
        )
        b = HistoryState(
            6,
            {root.sha: root, right.sha: right, expanded.sha: expanded},
            {
                "cloud": CopyRecord(
                    shared.sha, 6, "2026-01-02T00:00:00+00:00", "b"
                )
            },
        )
        merged = merge_states((a, b))
        self.assertEqual(
            set(merged.versions[shared.sha].parents), {left.sha, right.sha}
        )
        self.assertEqual(merged.copies["cloud"].host, "b")
        self.assertEqual(merged.generation, 6)

    def test_same_bytes_committed_by_two_hosts_merge_deterministically(
        self,
    ) -> None:
        root = version(1)
        left = version(
            2,
            (root.sha,),
            generation=3,
            host="b",
            time="2026-01-02T00:00:00+00:00",
        )
        right = version(
            2, (), generation=2, host="a", time="2026-01-03T00:00:00+00:00"
        )
        a = HistoryState(3, {root.sha: root, left.sha: left}, {})
        b = HistoryState(2, {right.sha: right}, {})
        for order in ((a, b), (b, a)):
            merged = merge_states(order).versions[left.sha]
            self.assertEqual((merged.generation, merged.host), (2, "a"))
            self.assertEqual(merged.parents, (root.sha,))
        different = Version(left.sha, (), 2, "a", left.time, size=101)
        with self.assertRaisesRegex(StateError, "conflicting immutable"):
            merge_states((a, HistoryState(2, {left.sha: different}, {})))

    def test_unknown_schema_is_refused(self) -> None:
        with self.assertRaisesRegex(StateError, "unsupported state schema"):
            HistoryState.from_dict(
                {
                    "schema_version": 9,
                    "generation": 0,
                    "versions": {},
                    "copies": {},
                }
            )


if __name__ == "__main__":
    unittest.main()
