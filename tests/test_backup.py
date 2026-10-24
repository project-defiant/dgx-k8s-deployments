"""Exercise the public CLI with disposable keys and files, never real data."""
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/platform-backup.py"


class BackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.TemporaryDirectory()
        os.chmod(cls.home.name, 0o700)
        cls.env = dict(os.environ, GNUPGHOME=cls.home.name)
        subprocess.run(["gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "", "--quick-generate-key", "Fixture <fixture@example.invalid>", "rsa2048", "encr", "0"], env=cls.env, check=True, capture_output=True)
        result = subprocess.run(["gpg", "--with-colons", "--list-keys"], env=cls.env, check=True, capture_output=True, text=True)
        cls.key = next(line.split(":")[9] for line in result.stdout.splitlines() if line.startswith("fpr:"))

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["gpgconf", "--kill", "gpg-agent"], env=cls.env, capture_output=True)
        cls.home.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "nested").mkdir()
        (self.source / "nested/data.txt").write_text("persistent fixture\n")
        self.archive = self.root / "fixture.gpg"
        self.target = self.root / "restored"

    def cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, ok, result.stderr)
        return result

    def backup(self, **kwargs):
        return self.cli("backup", self.source, self.archive, "--recipient", self.key, **kwargs)

    def test_roundtrip_and_existing_target_protection(self):
        self.backup()
        self.assertEqual(self.archive.stat().st_mode & 0o777, 0o600)
        self.cli("restore", self.archive, self.target)
        self.assertEqual((self.target / "nested/data.txt").read_text(), "persistent fixture\n")
        self.cli("restore", self.archive, self.target, ok=False)
        self.backup(ok=False)

    def test_tampered_ciphertext_never_extracted(self):
        self.backup()
        data = bytearray(self.archive.read_bytes())
        data[-15] ^= 1
        self.archive.write_bytes(data)
        self.cli("restore", self.archive, self.target, ok=False)
        self.assertFalse(self.target.exists())

    def test_traversal_and_links_rejected(self):
        for name, link in [("../escape", False), ("/absolute", False), ("link", True)]:
            plain = self.root / "malicious.tar"
            with tarfile.open(plain, "w") as tar:
                item = tarfile.TarInfo(name)
                if link:
                    item.type = tarfile.SYMTYPE
                    item.linkname = "../escape"
                    tar.addfile(item)
                else:
                    item.size = 1
                    tar.addfile(item, io.BytesIO(b"x"))
            self.archive.unlink(missing_ok=True)
            subprocess.run(["gpg", "--batch", "--trust-model", "always", "--recipient", self.key, "--output", str(self.archive), "--encrypt", str(plain)], env=self.env, check=True, capture_output=True)
            self.cli("restore", self.archive, self.target, ok=False)
            self.assertFalse(self.target.exists())
        self.assertFalse((self.root / "escape").exists())

    def test_unsafe_destination_and_source_links(self):
        for remote in ["user@host:/tmp/x;touch_bad", "-oProxyCommand=bad:/tmp/x", "user@host:/../x"]:
            self.cli("backup", self.source, self.archive, "--recipient", self.key, "--destination", remote, ok=False)
            self.assertFalse(self.archive.exists())
        (self.source / "link").symlink_to("/etc/passwd")
        self.backup(ok=False)
        self.assertFalse(self.archive.exists())

    def test_recipient_requires_full_fingerprint(self):
        for recipient in ['fixture@example.invalid', '01234567', '--help', 'G' * 40]:
            self.cli('backup', self.source, self.archive, '--recipient=' + recipient, ok=False)
            self.assertFalse(self.archive.exists())

    def test_empty_target_supported(self):
        self.backup()
        self.target.mkdir()
        self.cli("restore", self.archive, self.target)
        self.assertTrue((self.target / "nested/data.txt").exists())


if __name__ == "__main__":
    unittest.main()
