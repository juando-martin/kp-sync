import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from lxml import etree
from pykeepass import PyKeePass

from kp_sync.merge import merge_databases
from kp_sync.verify import verify_candidate


@unittest.skipUnless(
    shutil.which("keepassxc-cli"), "keepassxc-cli unavailable"
)
class CompatibilityTest(unittest.TestCase):
    def _run(self, *arguments, password="fixture-password\n"):
        result = subprocess.run(
            ["keepassxc-cli", *map(str, arguments)],
            input=password,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    @staticmethod
    def _import_password_options():
        # KeePassXC 2.7+ sets an import password only with -p
        # (--set-password); 2.6 prompts for one unconditionally.
        result = subprocess.run(
            ["keepassxc-cli", "import", "--help"],
            text=True,
            capture_output=True,
            timeout=30,
        )
        help_text = result.stdout + result.stderr
        return ["-p"] if "--set-password" in help_text else []

    def _canonical(self, xml):
        root = etree.fromstring(
            xml.encode(), etree.XMLParser(remove_blank_text=True)
        )
        for element in tuple(root.iter()):
            if element.tag in {
                "Generator",
                "HeaderHash",
                "SettingsChanged",
            } or (
                element.tag == "Item"
                and element.findtext("Key") == "_LAST_MODIFIED"
            ):
                element.getparent().remove(element)
        for data in root.iter("CustomData"):
            data[:] = sorted(data, key=lambda item: item.findtext("Key", ""))
        return etree.tostring(root, method="c14n")

    def test_keepassxc_fixture_roundtrip_and_merge(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            original = directory / "original.kdbx"
            fixture = Path(__file__).parent / "data" / "keepassxc.xml"
            self._run(
                "import",
                *self._import_password_options(),
                fixture,
                original,
                password="fixture-password\nfixture-password\n",
            )
            before = self._canonical(self._run("export", "-q", original))
            saved = directory / "saved.kdbx"
            database = PyKeePass(str(original), password="fixture-password")
            database.save(str(saved))
            self.assertEqual(
                before, self._canonical(self._run("export", "-q", saved))
            )
            merged = directory / "merged.kdbx"
            result = merge_databases(
                original,
                saved,
                original,
                merged,
                password="fixture-password",
            )
            self.assertTrue(result.merged)
            self.assertTrue(
                verify_candidate(
                    merged,
                    password="fixture-password",
                    require_keepassxc=True,
                ).verified
            )
            self.assertEqual(
                before, self._canonical(self._run("export", "-q", merged))
            )

    def test_kdbx3_merge_removes_stale_header_hash(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            original = directory / "legacy.kdbx"
            self._run(
                "db-create",
                "-p",
                original,
                password="fixture-password\nfixture-password\n",
            )
            database = PyKeePass(str(original), password="fixture-password")
            if database.version >= (4, 0):
                self.skipTest("installed KeePassXC creates KDBX4")
            result_path = directory / "merged.kdbx"
            result = merge_databases(
                original,
                original,
                original,
                result_path,
                password="fixture-password",
            )
            self.assertTrue(result.merged)
            self.assertTrue(
                verify_candidate(
                    result_path,
                    password="fixture-password",
                    require_keepassxc=True,
                ).verified
            )
