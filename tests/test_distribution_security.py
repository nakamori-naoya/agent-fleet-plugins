import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "validate-distribution.py"
SPEC = importlib.util.spec_from_file_location("validate_distribution", MODULE_PATH)
validate_distribution = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(validate_distribution)


class DistributionSelfTestSecurityTest(unittest.TestCase):
    def test_nested_secret_files_are_ignored(self):
        root = Path(__file__).parents[1]
        secret_paths = (
            "configs/private/.env",
            "plugins/example/nested/.env.production",
            "docs/internal/client.pem",
            "plugins/example/credential.key",
            "tests/fixtures/signing.p12",
            "configs/private/archive.pfx",
            "plugins/example/id_rsa",
            "plugins/example/id_ed25519",
        )
        for relative in secret_paths:
            with self.subTest(path=relative):
                result = subprocess.run(
                    ["git", "check-ignore", "--no-index", "--quiet", relative],
                    cwd=root,
                    check=False,
                )
                self.assertEqual(0, result.returncode)

    def test_shareable_environment_templates_are_not_ignored(self):
        root = Path(__file__).parents[1]
        for relative in (
            "configs/examples/.env.example",
            "plugins/example/.env.sample",
        ):
            with self.subTest(path=relative):
                result = subprocess.run(
                    ["git", "check-ignore", "--no-index", "--quiet", relative],
                    cwd=root,
                    check=False,
                )
                self.assertEqual(1, result.returncode)

    def test_self_test_rejects_another_root_before_copy(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            validate_distribution.shutil, "copytree"
        ) as copytree:
            with self.assertRaisesRegex(ValueError, "実行中のrepository root"):
                validate_distribution.validate_self_test_request(
                    Path(temporary).resolve()
                )

        copytree.assert_not_called()

    def test_self_test_sentinels_must_be_regular_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for relative in validate_distribution.SELF_TEST_SENTINELS:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n", encoding="utf-8")
            sentinel = root / validate_distribution.SELF_TEST_SENTINELS[0]
            target = root / "real-sentinel"
            target.write_text("fixture\n", encoding="utf-8")
            sentinel.unlink()
            sentinel.symlink_to(target)

            with self.assertRaisesRegex(ValueError, "regular file"):
                validate_distribution.validate_self_test_sentinels(root)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is unavailable")
    def test_self_test_rejects_special_file_before_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fifo = root / "untrusted-input"
            os.mkfifo(fifo)

            with self.assertRaisesRegex(ValueError, "特殊file"):
                validate_distribution.reject_unsafe_copy_entries(root)


if __name__ == "__main__":
    unittest.main()
