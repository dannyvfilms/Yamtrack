from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.test import SimpleTestCase

from config import settings


class SettingsMetadataTests(SimpleTestCase):
    """Test runtime/build metadata selection for version reporting."""

    def test_get_local_commit_hash_searches_parent_git_dir(self):
        """A normal repo checkout should resolve .git above BASE_DIR/src."""
        with TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir) / "repo"
            git_dir = repo_root / ".git"
            nested_dir = repo_root / "src" / "config"
            refs_dir = git_dir / "refs" / "heads"

            refs_dir.mkdir(parents=True)
            nested_dir.mkdir(parents=True)

            (git_dir / "HEAD").write_text("ref: refs/heads/latest\n")
            expected_sha = "1234567890abcdef1234567890abcdef12345678"
            (refs_dir / "latest").write_text(f"{expected_sha}\n")

            with mock.patch("config.settings.subprocess.run", side_effect=OSError):
                self.assertEqual(
                    settings._get_local_commit_hash(nested_dir),
                    expected_sha,
                )

    def test_read_git_ref_supports_packed_refs(self):
        """Packed refs should work when loose ref files are absent."""
        with TemporaryDirectory() as tmp_dir:
            git_dir = Path(tmp_dir) / ".git"
            git_dir.mkdir()
            (git_dir / "packed-refs").write_text(
                "# pack-refs with: peeled fully-peeled sorted\n"
                "abcdef1234567890abcdef1234567890abcdef12 refs/heads/latest\n",
            )

            self.assertEqual(
                settings._read_git_ref(git_dir, "refs/heads/latest"),
                "abcdef1234567890abcdef1234567890abcdef12",
            )

    def test_select_version_prefers_runtime_metadata_over_stale_env(self):
        """Runtime checkout metadata should win when env vars point at older code."""
        self.assertEqual(
            settings._select_version(
                "v26.4.2-7-ge0a9ee0c",
                "e0a9ee0c1234567890abcdef1234567890abcdef",
                "v0.24.9-2-31-g404a1ca4",
                "404a1ca4b65abac0eb262669bb93616f39c872dc",
            ),
            "v26.4.2-7-ge0a9ee0c",
        )

    def test_select_version_keeps_env_version_when_commit_matches(self):
        """Build metadata remains valid when it matches the running checkout."""
        commit_sha = "404a1ca4b65abac0eb262669bb93616f39c872dc"
        self.assertEqual(
            settings._select_version(
                None,
                commit_sha,
                "v0.24.9-2-31-g404a1ca4",
                commit_sha,
            ),
            "v0.24.9-2-31-g404a1ca4",
        )
