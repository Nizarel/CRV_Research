import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("transfer", Path(__file__).with_name("transfer.py"))
transfer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transfer)


class TransferTests(unittest.TestCase):
    def setUp(self):
        profile = patch.object(
            transfer, "request_json",
            return_value={"ocs": {"data": {"id": "canonical@example.com"}}},
        )
        self.profile = profile.start()
        self.addCleanup(profile.stop)

    def test_rejects_untrusted_origins(self):
        for url in (
            "http://myvdr.managemgroup.com:5443/login",
            "https://myvdr.managemgroup.com/login",
            "https://attacker.example:5443/login",
            "https://user:secret@myvdr.managemgroup.com:5443/login",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                transfer.trusted_url(url)

    def test_all_redirects_are_rejected(self):
        with self.assertRaises(RuntimeError):
            transfer.NoRedirects().redirect_request(None, None, 302, "", {}, "/other")

    def test_revokes_credentials_when_copy_fails(self):
        credentials = {
            "server": transfer.SERVER, "loginName": "test", "appPassword": "not-a-secret",
        }
        with (
            patch.object(transfer, "rclone", side_effect=RuntimeError("copy failed")),
            patch.object(transfer, "revoke") as revoke,
            self.assertRaisesRegex(RuntimeError, "copy failed"),
        ):
            transfer.transfer(credentials)
        revoke.assert_called_once()

    def test_empty_corpus_is_not_success(self):
        credentials = {
            "server": transfer.SERVER, "loginName": "test", "appPassword": "not-a-secret",
        }
        with (
            patch.object(transfer, "rclone", return_value="obscured"),
            patch.object(transfer, "inventory", return_value=[]),
            patch.object(transfer, "revoke") as revoke,
            self.assertRaisesRegex(RuntimeError, "empty"),
        ):
            transfer.transfer(credentials)
        revoke.assert_called_once()

    def test_verified_report_follows_content_check(self):
        credentials = {
            "server": transfer.SERVER, "loginName": "test", "appPassword": "not-a-secret",
        }
        entries = [{"Path": "a.pdf", "Size": 42, "ModTime": "2026-01-01"}]
        with (
            patch.object(transfer, "rclone", return_value="obscured") as rclone,
            patch.object(transfer, "inventory", return_value=entries),
            patch.object(transfer, "revoke"),
            patch.dict(transfer.os.environ, {"TRANSFER_JOB": "test-job"}),
        ):
            transfer.transfer(credentials)
        calls = [call.args for call in rclone.call_args_list]
        self.assertEqual([call[0] for call in calls], ["obscure", "lsf", "copy", "check", "rcat"])
        self.assertIn("--immutable", calls[2])
        self.assertIn("--download", calls[3])
        self.assertIn("--one-way", calls[3])
        self.assertEqual(
            rclone.call_args_list[2].kwargs["environment"]["RCLONE_CONFIG_SOURCE_URL"],
            transfer.SERVER + "/remote.php/dav/files/canonical%40example.com/",
        )

    def test_source_change_prevents_verified_report(self):
        credentials = {
            "server": transfer.SERVER, "loginName": "test", "appPassword": "not-a-secret",
        }
        with (
            patch.object(transfer, "rclone", return_value="obscured") as rclone,
            patch.object(transfer, "inventory", side_effect=[
                [{"Path": "a", "Size": 1, "ModTime": "before"}],
                [{"Path": "a", "Size": 2, "ModTime": "after"}],
            ]),
            patch.object(transfer, "revoke"),
            self.assertRaisesRegex(RuntimeError, "changed"),
        ):
            transfer.transfer(credentials)
        self.assertNotIn("rcat", [call.args[0] for call in rclone.call_args_list])


if __name__ == "__main__":
    unittest.main()
