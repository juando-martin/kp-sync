import json
import os
import tempfile
import unittest
from pathlib import Path

from kp_sync.pending import PendingError, PendingStore


class PendingStoreTest(unittest.TestCase):
    def test_saves_independent_decisions_and_resolves_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = PendingStore(temp_dir)
            store.save("merge", "conflicts need review", {"count": 2})
            store.save("drop", "entry drop needs confirmation")
            self.assertEqual(set(store.load()["decisions"]), {"merge", "drop"})

            self.assertTrue(store.resolve("merge"))
            self.assertFalse(store.resolve("missing"))
            self.assertEqual(set(store.load()["decisions"]), {"drop"})
            self.assertEqual(
                os.stat(Path(temp_dir) / "pending.json").st_mode & 0o777, 0o600
            )

    def test_choice_sets_are_keyed_by_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = PendingStore(temp_dir)
            store.save_choices("input-hash", {"entry:title": "other"})
            self.assertEqual(
                store.choices(),
                {"input-hash": {"entry:title": "other"}},
            )
            with self.assertRaises(ValueError):
                store.save_choices("input-hash", {"entry:title": "invalid"})

    def test_rejects_invalid_choice_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "choices.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "choices": {"hash": {"field": "secret"}},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(PendingError):
                PendingStore(temp_dir).choices()


if __name__ == "__main__":
    unittest.main()
