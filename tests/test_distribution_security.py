import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


# root 契約の validator は兄弟 checkout の harness-tools が正本（複製を持たない）
MODULE_PATH = Path(__file__).parents[2] / "harness-tools" / "tools" / "validate-plugin-repository.py"


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

    def test_symlinked_marketplace_is_rejected_through_the_public_cli(self):
        # harness-tools の validate-plugin-repository.py（root validator）の公開CLIで、
        # marketplace catalog が symlink のとき regular file 違反として非0で止まることを確かめる。
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "repository"
            shutil.copytree(
                root,
                copied,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
            )
            catalog = copied / ".agents/plugins/marketplace.json"
            real = copied / "real-catalog.json"
            real.write_bytes(catalog.read_bytes())
            catalog.unlink()
            catalog.symlink_to(real)
            result = subprocess.run(
                [sys.executable, str(MODULE_PATH), str(copied)],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(1, result.returncode)
        self.assertIn("regular file", result.stderr)

    def test_self_test_without_repository_runs_only_synthetic_fixtures(self):
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--self-test"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
