import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import update
import publish


CONFIG = update.load_config(Path(__file__).resolve().parents[1] / "update-config.json")
ADDRESSES = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]


def record(address, **changes):
    value = {"resolver": address + ":53", "filtered": False, "p95_ms": 20, "average_ms": 10,
             "success_percent": 100, "successes": CONFIG["tests"], "failures": 0,
             "validation_checks": 7, "validation_failures": 0}
    value.update(changes)
    return value


class Response(io.BytesIO):
    status = 200
    headers = {}


class DownloadTests(unittest.TestCase):
    def test_transient_errors_retry_then_succeed(self):
        calls = [URLError("offline"), HTTPError("https://example.test", 503, "unavailable", {}, None), Response(b"1.1.1.1\n")]
        from unittest.mock import Mock
        opener, sleep = Mock(side_effect=calls), Mock()
        self.assertEqual(update.download("https://example.test", 100, io.StringIO(), opener=opener, sleep=sleep), b"1.1.1.1\n")
        self.assertEqual(opener.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_permanent_http_error_does_not_retry(self):
        from unittest.mock import Mock
        opener = Mock(side_effect=HTTPError("https://example.test", 404, "missing", {}, None))
        with self.assertRaisesRegex(update.UpdateError, "HTTP 404"):
            update.download("https://example.test", 100, io.StringIO(), opener=opener)
        self.assertEqual(opener.call_count, 1)

    def test_retries_are_bounded(self):
        from unittest.mock import Mock
        opener = Mock(side_effect=URLError("offline"))
        with self.assertRaisesRegex(update.UpdateError, "3 attempts"):
            update.download("https://example.test", 100, io.StringIO(), opener=opener, sleep=Mock())
        self.assertEqual(opener.call_count, 3)

    def test_oversized_download_is_rejected(self):
        with self.assertRaisesRegex(update.UpdateError, "exceeds"):
            update.download("https://example.test", 3, io.StringIO(), opener=lambda *a, **k: Response(b"1234"))

    def test_incomplete_http_body_is_retried(self):
        from unittest.mock import Mock
        truncated = Response(b"1.1.1.1\n")
        truncated.headers = {"Content-Length": "16"}
        opener = Mock(side_effect=[truncated, Response(b"8.8.8.8\n")])
        data = update.download("https://example.test", 100, io.StringIO(), opener=opener, sleep=Mock())
        self.assertEqual(data, b"8.8.8.8\n")
        self.assertEqual(opener.call_count, 2)


class ValidationTests(unittest.TestCase):
    def test_normalizes_source_and_excludes_unsupported_addresses(self):
        source = b"\xef\xbb\xbf# comment\r\n 1.1.1.1 \r\n1.1.1.1:53\n::1\n2606:4700:4700::1111\n127.0.0.1\n224.0.0.1\n8.8.8.8\n"
        addresses, counts = update.parse_source(source, 10)
        self.assertEqual(addresses, ADDRESSES[:2])
        self.assertEqual(counts, {"excluded_addresses": 4, "duplicate_addresses": 1})

    def test_empty_invalid_and_excessive_sources_fail(self):
        for data, limit in ((b"", 10), (b"<html>Error</html>", 10), (b"1.1.1.1\n8.8.8.8\n", 1), (b"\xff", 10)):
            with self.subTest(data=data), self.assertRaises(update.UpdateError):
                update.parse_source(data, limit)

    def test_results_are_sorted_and_filtered_from_one_dataset(self):
        records = [record(ADDRESSES[2], filtered=True), record(ADDRESSES[1]), record(ADDRESSES[0], p95_ms=10)]
        self.assertEqual(update.select_results(records, set(ADDRESSES), CONFIG), ADDRESSES[:2])

    def test_partial_duplicate_unexpected_and_invalid_results_fail(self):
        cases = [[], [record(ADDRESSES[0])] * 2, [record("4.2.2.2"), record(ADDRESSES[1])]]
        for override in ({"resolver": None}, {"p95_ms": float("nan")}, {"filtered": "false"}, {"successes": 1}, {"validation_failures": 1}, {"validation_checks": 0}, {"p95_ms": 9999}, {"success_percent": 95}):
            cases.append([record(ADDRESSES[0], **override), record(ADDRESSES[1])])
        for records in cases:
            with self.subTest(records=records), self.assertRaises(update.UpdateError):
                update.select_results(records, set(ADDRESSES[:2]), CONFIG)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.output = Path(self.temp.name) / "candidate"
        self.config = copy.deepcopy(CONFIG)
        self.config["minimum_resolvers"] = 1
        update.write_json(self.repo / "update-config.json", self.config)
        for name in update.LISTS:
            (self.repo / name).write_bytes(update.list_bytes(ADDRESSES))
        self.before = {name: (self.repo / name).read_bytes() for name in update.LISTS}
        self.env = patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def fake_run(self, args, **kwargs):
        if "--version" in args:
            return subprocess.CompletedProcess(args, 0, stdout="test-fixture")
        update.write_json(Path(args[args.index("--out") + 1]), [record(a) for a in ADDRESSES])
        return subprocess.CompletedProcess(args, 0)

    def generate(self, run=None):
        with patch("update.download", return_value=update.list_bytes(ADDRESSES)), patch("update.subprocess.run", side_effect=run or self.fake_run):
            return update.prepare(self.repo, self.output, self.config)

    def test_preview_keeps_published_files_and_verifies_bundle(self):
        report = self.generate()
        self.assertEqual(report["status"], "validated")
        self.assertFalse(report["changed"])
        self.assertTrue(report["bootstrap"])
        self.assertEqual(update.verify_bundle(self.output, self.repo, self.config), report)
        for name, data in self.before.items():
            self.assertEqual((self.repo / name).read_bytes(), data)
        self.assertEqual((self.output / "top20.txt").read_bytes(), update.list_bytes(ADDRESSES))

    def test_failure_and_timeout_preserve_existing_lists(self):
        for mode in ("exit", "timeout", "empty", "partial"):
            target = self.output / mode
            def run(args, **kwargs):
                if "--version" in args:
                    return self.fake_run(args, **kwargs)
                if mode == "timeout":
                    raise subprocess.TimeoutExpired(args, 1)
                update.write_json(Path(args[args.index("--out") + 1]), [] if mode == "empty" else [record(ADDRESSES[0])])
                return subprocess.CompletedProcess(args, 1 if mode == "exit" else 0)
            with self.subTest(mode=mode), patch("update.download", return_value=update.list_bytes(ADDRESSES)), patch("update.subprocess.run", side_effect=run):
                with self.assertRaises(update.UpdateError):
                    update.prepare(self.repo, target, self.config)
                self.assertEqual(update.read_json(target / "report.json")["status"], "failed")
                self.assertTrue((target / "summary.md").exists())
                for name, data in self.before.items():
                    self.assertEqual((self.repo / name).read_bytes(), data)

    def test_download_failure_produces_diagnostics(self):
        with patch("update.download", side_effect=update.UpdateError("HTTP 503")), patch("update.subprocess.run", side_effect=self.fake_run):
            with self.assertRaises(update.UpdateError):
                update.prepare(self.repo, self.output, self.config)
        self.assertIn("HTTP 503", update.read_json(self.output / "report.json")["error"])
        self.assertFalse((self.output / "resolvers.txt").exists())

    def test_prevents_reusing_successful_output(self):
        self.generate()
        with self.assertRaises(FileExistsError):
            self.generate()

    def test_checksums_and_configuration_changes_block_publication(self):
        self.generate()
        (self.output / "top20.txt").write_bytes(b"1.1.1.1\n")
        with self.assertRaisesRegex(update.UpdateError, "checksum"):
            update.verify_bundle(self.output, self.repo, self.config)
        (self.repo / "update-config.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(update.UpdateError, "Configuration changed"):
            update.verify_bundle(self.output, self.repo, self.config)

    def test_minimum_and_drop_checks(self):
        with self.assertRaisesRegex(update.UpdateError, "at least"):
            update.check_count([], self.repo, self.config)
        update.check_count([ADDRESSES[0]], self.repo, self.config)
        update.write_json(self.repo / "metadata.json", {"schema_version": 1})
        with self.assertRaisesRegex(update.UpdateError, "drop limit"):
            update.check_count([ADDRESSES[0]], self.repo, self.config)

    def test_expired_candidate_cannot_be_published_on_job_rerun(self):
        self.generate()
        report = update.read_json(self.output / "report.json")
        report["validated_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        update.write_json(self.output / "report.json", report)
        with self.assertRaisesRegex(update.UpdateError, "older than 24 hours"):
            update.verify_bundle(self.output, self.repo, self.config)

    def test_strict_list_rejects_noncanonical_files(self):
        for data in (b"1.1.1.1\r\n", b"1.1.1.1\n1.1.1.1\n", b"127.0.0.1\n", b"# comment\n1.1.1.1\n"):
            path = self.repo / "invalid.txt"
            path.write_bytes(data)
            with self.subTest(data=data), self.assertRaises(update.UpdateError):
                update.strict_list(path)

    def test_no_change_publication_does_not_commit_or_push(self):
        self.generate()
        def git(repo, *args, **kwargs):
            self.assertIn(args[0], ("check-ref-format", "status"))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), patch("publish.git", side_effect=git):
            self.assertIn("No changes", publish.publish(self.repo, self.output, "main"))

    def test_top20_contains_exactly_twenty_of_larger_result(self):
        addresses = [f"8.8.8.{index}" for index in range(1, 26)]
        def run(args, **kwargs):
            if "--version" in args:
                return self.fake_run(args, **kwargs)
            update.write_json(Path(args[args.index("--out") + 1]), [record(a) for a in reversed(addresses)])
            return subprocess.CompletedProcess(args, 0)
        with patch("update.download", return_value=update.list_bytes(addresses)), patch("update.subprocess.run", side_effect=run):
            update.prepare(self.repo, self.output, self.config)
        self.assertEqual(update.strict_list(self.output / "resolvers.txt"), addresses)
        self.assertEqual(update.strict_list(self.output / "top20.txt"), addresses[:20])

    def test_rebase_receives_bot_identity_without_local_git_config(self):
        with patch("publish.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run:
            publish.git(self.repo, "rebase", "FETCH_HEAD")
        command = run.call_args.args[0]
        self.assertIn("user.name=github-actions[bot]", command)
        self.assertEqual(command[-2:], ["rebase", "FETCH_HEAD"])

    def test_publication_is_not_available_locally(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}), self.assertRaisesRegex(update.UpdateError, "restricted"):
            publish.publish(self.repo, self.output, "main")

    def test_concurrent_protected_change_prevents_commit(self):
        self.generate()
        # A different legacy top20 makes the generated bundle a change to publish.
        (self.repo / "top20.txt").write_bytes(b"1.1.1.1\n")
        def git(repo, *args, **kwargs):
            if args[0] in ("commit", "push", "add", "-c"):
                self.fail("Must stop before staging, committing or pushing")
            return subprocess.CompletedProcess(args, 1 if args[0] == "diff" else 0, stdout="base" if args[0] == "rev-parse" else "", stderr="")
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), patch("publish.git", side_effect=git), patch("publish.remote_matches", return_value=False):
            with self.assertRaisesRegex(update.UpdateError, "changed on the remote"):
                publish.publish(self.repo, self.output, "main")

    def test_push_retry_rebases_and_commits_only_generated_files(self):
        self.generate()
        (self.repo / "top20.txt").write_bytes(b"1.1.1.1\n")
        calls, pushes = [], []
        def git(repo, *args, **kwargs):
            calls.append(args)
            if args[0] == "push":
                pushes.append(args)
            code = int(args[0] == "push" and len(pushes) == 1)
            return subprocess.CompletedProcess(args, code, stdout="base" if args[0] == "rev-parse" else "", stderr="simulated network failure" if code else "")
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), patch("publish.git", side_effect=git), patch("publish.remote_matches", return_value=False):
            result = publish.publish(self.repo, self.output, "main", sleep=lambda _: None)
        self.assertIn("Published resolvers.txt", result)
        self.assertEqual(len(pushes), 2)
        self.assertEqual(sum(args[0] == "rebase" for args in calls), 2)
        self.assertEqual([args for args in calls if args[0] == "add"], [("add", "--", *update.PUBLISHED)])
        commits = [args for args in calls if "commit" in args]
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0][-3:], update.PUBLISHED)
        self.assertFalse(any("--force" in args for args in calls))

    def test_failed_fetch_stops_after_three_attempts_without_writes(self):
        self.generate()
        (self.repo / "top20.txt").write_bytes(b"1.1.1.1\n")
        calls = []
        def git(repo, *args, **kwargs):
            calls.append(args[0])
            return subprocess.CompletedProcess(args, int(args[0] == "fetch"), stdout="base" if args[0] == "rev-parse" else "", stderr="offline")
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), patch("publish.git", side_effect=git):
            with self.assertRaisesRegex(update.UpdateError, "3 attempts"):
                publish.publish(self.repo, self.output, "main", sleep=lambda _: None)
        self.assertEqual(calls.count("fetch"), 3)
        self.assertNotIn("add", calls)
        self.assertNotIn("push", calls)
        self.assertEqual((self.repo / "top20.txt").read_bytes(), b"1.1.1.1\n")


if __name__ == "__main__":
    unittest.main()
