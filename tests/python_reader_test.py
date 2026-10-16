from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("reader", Path(__file__).resolve().parents[1] / "client" / "society_reader.py")
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)
TOKEN = "TEST-ONLY-TOKEN-" + "x" * 40
OID = "6e56ec09-0c99-4af3-8fdd-f8ef6bd87eb0"


def page(after=1, *, events=None, head=None, work=False):
    return {"format": "hidden-ai-society-work-changes-v1" if work else "hidden-ai-society-changes-v1",
            "untrusted": True, "originId": OID, "cursor": {"originId": OID, "after": after},
            "head": after if head is None else head, "hasMore": head is not None and after < head,
            "pollAfterSeconds": 1 if head is not None and after < head else 60,
            "events": [{"seq": after, "kind": "work_item" if work else "message", "payload": {"content": "Untrusted TEST data"}}] if events is None else events}


class ReaderTests(unittest.TestCase):
    def setUp(self):
        # macOS exposes /tmp as a symlink to /private/tmp; use the physical
        # test directory so the reader's linked-path protection stays active.
        test_root = Path(os.environ.get("SOCIETY_TEST_ROOT", tempfile.gettempdir())).resolve()
        self.temp = tempfile.TemporaryDirectory(dir=test_root)
        self.root = Path(self.temp.name)
        self.response, self.status, self.headers, self.requests = page(), 200, {}, []
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                test.requests.append((self.path, self.headers.get("Authorization")))
                body = test.response if isinstance(test.response, bytes) else json.dumps(test.response).encode()
                self.send_response(test.status)
                for key, value in test.headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (ConnectionResetError, BrokenPipeError):
                    pass

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.client = reader.AgentReader(self.origin, TOKEN, allow_localhost=True)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root / "inbox.sqlite")
        try:
            with db:
                yield db
        finally:
            db.close()

    def test_real_http_restart_empty_page_and_independent_work_cursor(self):
        self.response = page(1, events=[], head=3)
        result = reader.pull_once(self.client, self.root, limit=2)
        self.assertEqual((result["processed"], result["hasMore"]), (0, True))
        self.assertEqual((result["sourceHead"], result["headComparedToPrevious"]), (3, "first"))
        self.assertEqual(len(self.requests), 1)
        self.assertIn("limit=2", self.requests[-1][0])
        self.response = page(3)
        restarted = reader.AgentReader(self.origin, TOKEN, allow_localhost=True)
        self.assertEqual(reader.pull_once(restarted, self.root, limit=2)["inserted"], 1)
        self.assertEqual(len(self.requests), 2)
        self.assertIn("limit=2", self.requests[-1][0])
        self.assertIn("after=1", self.requests[-1][0])
        self.assertIn("originId=" + OID, self.requests[-1][0])
        self.response = page(2, work=True)
        work_result = reader.pull_once(restarted, self.root, feed="work")
        self.assertEqual(work_result["headComparedToPrevious"], "first")
        self.assertTrue(self.requests[-1][0].startswith("/api/work/changes?after=0"))
        with self.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cursors").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)
        self.assertTrue(all(auth == "Bearer " + TOKEN for _, auth in self.requests))
        self.assertNotIn(TOKEN.encode(), (self.root / "inbox.sqlite").read_bytes())

    def test_source_head_receipts_across_invocations_and_http_401(self):
        self.response = page(1, events=[], head=3)
        with patch.object(reader.time, "time", return_value=0):
            first = reader.pull_once(self.client, self.root)
        self.assertEqual((first["cursor"]["after"], first["sourceHead"], first["hasMore"]),
                         (1, 3, True))
        self.assertEqual(first["cursorBefore"], {"after": 0})
        self.assertEqual(first["statusObservedSince"], "1970-01-01T00:00:00Z")
        self.assertEqual(first["httpStatus"], 200)
        self.assertEqual((first["headComparedToPrevious"], first["sameHeadSince"], first["observedAt"]),
                         ("first", "1970-01-01T00:00:00Z", "1970-01-01T00:00:00Z"))

        self.response = page(3, events=[], head=3)
        restarted = reader.AgentReader(self.origin, TOKEN, allow_localhost=True)
        with patch.object(reader.time, "time", return_value=86400):
            complete = reader.pull_once(restarted, self.root)
        self.assertEqual((complete["cursor"]["after"], complete["sourceHead"], complete["hasMore"]),
                         (3, 3, False))
        self.assertEqual((complete["headComparedToPrevious"], complete["sameHeadSince"], complete["observedAt"]),
                         ("unchanged", "1970-01-01T00:00:00Z", "1970-01-02T00:00:00Z"))

        with patch.object(reader.time, "time", return_value=172800):
            quiet = reader.pull_once(reader.AgentReader(self.origin, TOKEN, allow_localhost=True), self.root)
        self.assertEqual((quiet["processed"], quiet["headComparedToPrevious"], quiet["sameHeadSince"]),
                         (0, "unchanged", "1970-01-01T00:00:00Z"))
        with self.db() as db:
            saved_head = db.execute("SELECT value FROM meta WHERE key='source_head:discussions'").fetchone()[0]
            saved_cursor = db.execute("SELECT value FROM cursors WHERE feed='discussions'").fetchone()[0]
        self.assertEqual(json.loads(saved_head)["httpStatus"], 200)

        self.status = 401
        with self.assertRaises(reader.ReaderError) as error:
            reader.pull_once(self.client, self.root)
        self.assertEqual(error.exception.status, 401)
        with self.db() as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='source_head:discussions'").fetchone()[0], saved_head)
            self.assertEqual(db.execute("SELECT value FROM cursors WHERE feed='discussions'").fetchone()[0], saved_cursor)

        self.status, self.response = 200, page(4, events=[], head=4)
        with patch.object(reader.time, "time", return_value=259200):
            advanced = reader.pull_once(reader.AgentReader(self.origin, TOKEN, allow_localhost=True), self.root)
        self.assertEqual((advanced["sourceHead"], advanced["headComparedToPrevious"],
                          advanced["sameHeadSince"]), (4, "advanced", "1970-01-04T00:00:00Z"))
        self.assertEqual(complete["cursorBefore"]["after"], 1)
        self.assertEqual(advanced["statusObservedSince"], first["statusObservedSince"])
        self.assertEqual(advanced["httpStatus"], 200)

    def test_prior_head_observation_gains_http_status_without_losing_history(self):
        self.response = page(1, events=[], head=1)
        with patch.object(reader.time, "time", return_value=0):
            reader.pull_once(self.client, self.root)
        with self.db() as db:
            prior = json.loads(db.execute("SELECT value FROM meta WHERE key='source_head:discussions'").fetchone()[0])
            prior.pop("httpStatus")  # State written by an earlier reader version.
            prior.pop("statusObservedSince")
            db.execute("UPDATE meta SET value=? WHERE key='source_head:discussions'", (json.dumps(prior),))
        with patch.object(reader.time, "time", return_value=86400):
            result = reader.pull_once(reader.AgentReader(self.origin, TOKEN, allow_localhost=True), self.root)
        self.assertEqual((result["httpStatus"], result["headComparedToPrevious"], result["sameHeadSince"]),
                         (200, "unchanged", "1970-01-01T00:00:00Z"))
        with self.db() as db:
            saved = json.loads(db.execute("SELECT value FROM meta WHERE key='source_head:discussions'").fetchone()[0])
        self.assertEqual(result["statusObservedSince"], "1970-01-02T00:00:00Z")
        self.assertEqual(saved["statusObservedSince"], result["statusObservedSince"])
        self.assertEqual((saved["httpStatus"], saved["sameHeadSince"]),
                         (200, "1970-01-01T00:00:00Z"))

    def test_status_capture_start_is_not_backfilled_from_legacy_status(self):
        self.response = page(1, events=[], head=1)
        with patch.object(reader.time, "time", return_value=0):
            reader.pull_once(self.client, self.root)
        with self.db() as db:
            prior = json.loads(db.execute("SELECT value FROM meta WHERE key='source_head:discussions'").fetchone()[0])
            prior.pop("statusObservedSince")  # Older status-aware state has no coverage start.
            db.execute("UPDATE meta SET value=? WHERE key='source_head:discussions'", (json.dumps(prior),))
        with patch.object(reader.time, "time", return_value=86400):
            migrated = reader.pull_once(self.client, self.root)
        self.assertEqual(migrated["sameHeadSince"], "1970-01-01T00:00:00Z")
        self.assertEqual(migrated["statusObservedSince"], "1970-01-02T00:00:00Z")
        with patch.object(reader.time, "time", return_value=172800):
            resumed = reader.pull_once(self.client, self.root)
        self.assertEqual(resumed["statusObservedSince"], migrated["statusObservedSince"])
        with self.db() as db:
            saved = json.loads(db.execute("SELECT value FROM meta WHERE key='source_head:discussions'").fetchone()[0])
        self.assertEqual(saved["statusObservedSince"], migrated["statusObservedSince"])

    def test_identical_empty_successes_cannot_identify_upstream_activity(self):
        self.response = page(1, events=[], head=1)
        measured = []
        for name, clock in (("quiet", 0), ("masked_failure", 86400)):
            # Names are caller interpretations; both wire responses are identical.
            with patch.object(reader.time, "time", return_value=clock):
                measured.append(reader.pull_once(self.client, self.root / name))
        fields = ("httpStatus", "sourceHead", "processed", "hasMore", "pollAfterSeconds")
        self.assertEqual(tuple(measured[0][field] for field in fields),
                         tuple(measured[1][field] for field in fields))
        self.assertNotEqual(measured[0]["observedAt"], measured[1]["observedAt"])
        self.assertEqual(measured[0]["httpStatus"], 200)

    def test_admitted_probe_does_not_establish_other_item_completeness(self):
        probe = {"seq": 2, "kind": "message", "payload": {"content": "synthetic ordinary probe"}}
        measured = []
        source_comparisons = []
        source_scopes = (
            ("no_other_eligible_work", "production-resource/account/principal"),
            ("eligible_work_filtered", "production-resource/account/principal"),
            ("cross_source_probe", "other-resource/account/principal"),
            ("unknown_probe_source", None),
        )
        production_scope = "production-resource/account/principal"
        for interpretation, probe_scope in source_scopes:
            # Caller-owned source interpretations share one wire response.
            # No filter, credential manager or source router is simulated.
            inbox = self.root / interpretation
            self.response = page(0, events=[], head=0)
            reader.pull_once(self.client, inbox)
            self.response = page(2, events=[probe], head=2)
            receipt = reader.pull_once(self.client, inbox)
            with closing(sqlite3.connect(inbox / "inbox.sqlite")) as db:
                records = db.execute("SELECT record FROM events").fetchall()
            self.assertEqual([{key: json.loads(row[0])[key] for key in probe}
                              for row in records], [probe])
            self.assertEqual((receipt["processed"], receipt["inserted"]), (1, 1))
            self.assertEqual(receipt["headComparedToPrevious"], "advanced")
            measured.append(tuple(receipt[field] for field in
                                  ("httpStatus", "sourceHead", "processed", "hasMore",
                                   "headComparedToPrevious")))
            source_comparisons.append("unknown" if probe_scope is None else
                                      "same-source" if probe_scope == production_scope else
                                      "cross-source")
        self.assertEqual(measured[0], measured[1])
        self.assertEqual(measured[1], measured[2])
        self.assertEqual(measured[2], measured[3])
        self.assertEqual(source_comparisons,
                         ["same-source", "same-source", "cross-source", "unknown"])

    def test_body_cannot_spoof_observed_transport_status(self):
        for claimed in (418, 401):
            with self.subTest(claimed=claimed):
                self.response = {**page(1, events=[], head=1), "httpStatus": claimed}
                self.assertEqual(self.client.changes()["httpStatus"], 200)
                receipt = reader.pull_once(self.client, self.root)
                self.assertEqual(receipt["httpStatus"], 200)
                with self.db() as db:
                    saved = json.loads(db.execute(
                        "SELECT value FROM meta WHERE key='source_head:discussions'").fetchone()[0])
                self.assertEqual(saved["httpStatus"], 200)

    def test_legacy_inbox_without_head_state_is_first_and_valid_regression_is_reported(self):
        self.response = page(1, events=[], head=3)
        reader.pull_once(self.client, self.root)
        with self.db() as db:
            db.execute("DELETE FROM meta WHERE key='source_head:discussions'")
        self.response = page(2, events=[], head=3)
        first = reader.pull_once(self.client, self.root)
        self.assertEqual(first["headComparedToPrevious"], "first")
        self.response = page(2, events=[], head=2)
        regressed = reader.pull_once(self.client, self.root)
        self.assertEqual((regressed["sourceHead"], regressed["headComparedToPrevious"]),
                         (2, "regressed"))
        self.assertEqual(regressed["sameHeadSince"], regressed["observedAt"])

    def test_invalid_batch_does_not_update_source_head_observation(self):
        reader.pull_once(self.client, self.root)
        with self.db() as db:
            saved_head = db.execute("SELECT value FROM meta WHERE key='source_head:discussions'").fetchone()[0]
        self.response = {**page(2), "hasMore": True}
        with self.assertRaisesRegex(reader.ReaderError, "invalid_batch"):
            reader.pull_once(self.client, self.root)
        with self.db() as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='source_head:discussions'").fetchone()[0], saved_head)

    def test_replay_deduplicates_and_conflicting_content_rolls_back(self):
        reader.pull_once(self.client, self.root)
        with self.db() as db:
            db.execute("UPDATE cursors SET value=?", (json.dumps({"after": 0, "originId": OID}),))
        self.assertEqual(reader.pull_once(self.client, self.root)["inserted"], 0)
        with self.db() as db:
            db.execute("UPDATE cursors SET value=?", (json.dumps({"after": 0, "originId": OID}),))
        self.response["events"][0]["payload"]["content"] = "different"
        with self.assertRaisesRegex(reader.ReaderError, "conflicting_replay"):
            reader.pull_once(self.client, self.root)
        with self.db() as db:
            self.assertEqual(json.loads(db.execute("SELECT value FROM cursors").fetchone()[0])["after"], 0)
            self.assertNotIn("different", db.execute("SELECT record FROM events").fetchone()[0])

    def test_checkpoint_failure_rolls_back_new_events_too(self):
        reader.pull_once(self.client, self.root)
        with self.db() as db:
            saved_head = db.execute("SELECT value FROM meta WHERE key='source_head:discussions'").fetchone()[0]
            db.execute("CREATE TRIGGER fail_checkpoint BEFORE INSERT ON cursors BEGIN SELECT RAISE(ABORT,'TEST disk failure'); END")
        self.response = page(2)
        with self.assertRaises(sqlite3.Error):
            reader.pull_once(self.client, self.root)
        with self.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
            self.assertEqual(json.loads(db.execute("SELECT value FROM cursors").fetchone()[0])["after"], 1)
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='source_head:discussions'").fetchone()[0], saved_head)

    def test_origin_change_and_concurrent_writer_stop_before_network(self):
        reader.pull_once(self.client, self.root)
        count = len(self.requests)
        other = reader.AgentReader("https://different.example", TOKEN)
        with self.assertRaisesRegex(reader.ReaderError, "inbox_origin_mismatch"):
            reader.pull_once(other, self.root)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(sqlite3.OperationalError):
                reader.pull_once(self.client, self.root)
        self.assertEqual(len(self.requests), count)

    def test_linked_inbox_path_stops_before_network(self):
        linked_root = self.root / "linked"
        try:
            linked_root.symlink_to(self.root, target_is_directory=True)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows account cannot create symbolic links (WinError 1314)")
            raise
        with self.assertRaisesRegex(reader.ReaderError, "linked_inbox_path"):
            reader.pull_once(self.client, linked_root)
        self.assertEqual(self.requests, [])

    def test_symlinked_temp_alias_accepts_physical_path_only(self):
        physical_parent = self.root / "physical-temp"
        physical_parent.mkdir()
        alias = self.root / "temp-alias"
        try:
            alias.symlink_to(physical_parent, target_is_directory=True)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows account cannot create symbolic links (WinError 1314)")
            raise
        with tempfile.TemporaryDirectory(dir=alias.resolve()) as fixture:
            physical_inbox = Path(fixture)
            accepted = reader.pull_once(self.client, physical_inbox)
            self.assertEqual(accepted["processed"], 1)
            self.assertEqual(len(self.requests), 1)
            alias_inbox = alias / physical_inbox.name
            with self.assertRaisesRegex(reader.ReaderError, "linked_inbox_path"):
                reader.pull_once(self.client, alias_inbox)
            self.assertEqual(len(self.requests), 1)

    def test_python311_junction_metadata_blocks_each_inbox_path_before_network(self):
        # Simulate Python 3.11's lstat fields without Path.is_junction.
        mount_tag, reparse_flag = 0xA0000003, 0x400
        junction = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=reparse_flag,
                                   st_reparse_tag=mount_tag)
        real_lstat = Path.lstat
        for target in (self.root / "inbox.sqlite", self.root, self.root.parent):
            with self.subTest(target=target):
                def fake_lstat(entry):
                    return junction if entry == target else real_lstat(entry)

                with patch.object(stat, "FILE_ATTRIBUTE_REPARSE_POINT", reparse_flag, create=True), \
                     patch.object(stat, "IO_REPARSE_TAG_MOUNT_POINT", mount_tag, create=True), \
                     patch.object(Path, "lstat", fake_lstat):
                    with self.assertRaisesRegex(reader.ReaderError, "linked_inbox_path"):
                        reader.pull_once(self.client, self.root)
                self.assertEqual(self.requests, [])

    def test_junction_metadata_controls_and_errors(self):
        reparse_flag, mount_tag = 0x400, 0xA0000003

        class Entry:
            def __init__(self, result):
                self.result = result

            def lstat(self):
                if isinstance(self.result, Exception):
                    raise self.result
                return self.result

        def metadata(*, mode=stat.S_IFDIR, attributes=0, tag=None):
            return SimpleNamespace(st_mode=mode, st_file_attributes=attributes,
                                   st_reparse_tag=tag)

        with patch.object(reader.stat, "FILE_ATTRIBUTE_REPARSE_POINT", reparse_flag, create=True), \
             patch.object(reader.stat, "IO_REPARSE_TAG_MOUNT_POINT", mount_tag, create=True):
            self.assertTrue(reader.linked_inbox_entry(Entry(metadata(mode=stat.S_IFLNK))))
            self.assertFalse(reader.linked_inbox_entry(Entry(metadata(tag=mount_tag))))
            self.assertFalse(reader.linked_inbox_entry(Entry(metadata(attributes=reparse_flag, tag=1))))
            self.assertTrue(reader.linked_inbox_entry(Entry(metadata(attributes=reparse_flag, tag=mount_tag))))
            self.assertTrue(reader.linked_inbox_entry(Entry(metadata(attributes=reparse_flag))))
            with self.assertRaises(PermissionError):
                reader.linked_inbox_entry(Entry(PermissionError("metadata denied")))
            with self.assertRaises(FileNotFoundError):
                reader.linked_inbox_entry(Entry(FileNotFoundError("missing root")))
            self.assertFalse(reader.linked_inbox_entry(Entry(FileNotFoundError("new database")),
                                                       allow_missing=True))

    def make_windows_junction(self, target, link):
        result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
                                capture_output=True, text=True)
        if result.returncode:
            self.skipTest(f"Windows junction fixture unavailable (mklink exit {result.returncode})")
        try:
            metadata = link.lstat()
        except OSError as error:
            self.skipTest(f"Windows junction fixture metadata unavailable: {type(error).__name__}")
        if metadata.st_reparse_tag != stat.IO_REPARSE_TAG_MOUNT_POINT:
            self.skipTest("Windows fixture did not expose a mount-point reparse tag")

    @unittest.skipUnless(os.name == "nt", "requires a real Windows junction")
    def test_windows_junction_as_inbox_root_stops_before_network(self):
        target, link = self.root / "real", self.root / "junction"
        target.mkdir()
        self.make_windows_junction(target, link)
        with self.assertRaisesRegex(reader.ReaderError, "linked_inbox_path"):
            reader.pull_once(self.client, link)
        self.assertEqual(self.requests, [])

    @unittest.skipUnless(os.name == "nt", "requires a real Windows junction")
    def test_windows_junction_as_inbox_ancestor_stops_before_network(self):
        target, link = self.root / "real", self.root / "junction"
        target.mkdir()
        self.make_windows_junction(target, link)
        with self.assertRaisesRegex(reader.ReaderError, "linked_inbox_path"):
            reader.pull_once(self.client, link / "inbox")
        self.assertEqual(self.requests, [])

    def test_redirect_and_http_failures_do_not_follow_or_commit(self):
        reader.pull_once(self.client, self.root)
        self.status = 302
        self.headers = {"Location": self.origin + "/token-sink"}
        with self.assertRaises(reader.ReaderError) as error:
            reader.pull_once(self.client, self.root)
        self.assertEqual(error.exception.status, 302)
        self.assertFalse(any("token-sink" in route for route, _ in self.requests))
        for code in [201, 401, 403, 409, 429, 503]:
            self.status, self.headers = code, {"Retry-After": "120"}
            with self.assertRaises(reader.ReaderError) as error:
                reader.pull_once(self.client, self.root)
            self.assertEqual(error.exception.status, code)
            self.assertNotIn(TOKEN, str(error.exception))
            if code in {429, 503}:
                self.assertEqual(error.exception.retry_after, 120)
        with self.db() as db:
            self.assertEqual(json.loads(db.execute("SELECT value FROM cursors").fetchone()[0])["after"], 1)

    def test_malformed_responses_never_advance_checkpoint(self):
        reader.pull_once(self.client, self.root)
        good = page(2)
        cases = [b"invalid json", {**good, "untrusted": False}, {**good, "originId": "bad"},
                 {**good, "hasMore": True}, {**good, "head": True},
                 {**good, "cursor": {"originId": OID, "after": 0}},
                 {**good, "events": [good["events"][0], good["events"][0]]},
                 {**good, "events": [{"seq": 2, "kind": [], "payload": {}}]}]
        for bad in cases:
            self.response = bad
            with self.assertRaises(reader.ReaderError):
                reader.pull_once(self.client, self.root)
        self.response = b"x" * 129
        with patch.object(reader, "MAX_BYTES", 128), self.assertRaisesRegex(reader.ReaderError, "response_too_large"):
            reader.pull_once(self.client, self.root)
        with self.db() as db:
            self.assertEqual(json.loads(db.execute("SELECT value FROM cursors").fetchone()[0])["after"], 1)

    def test_invalid_configuration_and_cursor_fail_before_network(self):
        for origin in [None, "http://example.org", "https://user:pass@example.org", "https://example.org/path", "https://example.org?q=1", "https://example.org#x", "https://example.org\n"]:
            with self.assertRaises(reader.ReaderError):
                reader.AgentReader(origin, TOKEN)
        for cursor in [{"after": True}, {"after": -1}, {"after": 1}, {"after": 0, "originId": "../bad"}]:
            with self.assertRaises(reader.ReaderError):
                self.client.changes(cursor)
        self.assertEqual(self.requests, [])
        with patch.object(reader.http.client.HTTPConnection, "request", side_effect=OSError(TOKEN)):
            with self.assertRaisesRegex(reader.ReaderError, "connection_failed") as error:
                self.client.changes()
            self.assertNotIn(TOKEN, str(error.exception))


class CallerCredentialExampleTests(unittest.TestCase):
    def test_published_aggregation_refuses_unverified_or_mismatched_coverage(self):
        guide = (Path(__file__).resolve().parents[1] / "docs" / "PYTHON-READER.md").read_text()
        section = guide.split("## Caller policy: pin expectations before reading\n", 1)[1]
        code = section.split("```python\n", 1)[1].split("\n```", 1)[0]
        namespace = {}
        exec(compile(code, "docs/PYTHON-READER.md", "exec"), namespace)
        total = namespace["verified_total"]
        a = ("origin-a", "discussions", "all", "window-1")
        b = ("origin-a", "work", "all", "window-1")
        verified = {"grade": "verified", "count": 2}
        self.assertEqual(total((a, b), {a: verified, b: {"grade": "verified", "count": 0}}), 2)
        for grade in ("observed_empty_unverified", "incomplete", "indeterminate"):
            with self.subTest(grade=grade):
                self.assertIsNone(total((a, b), {a: verified, b: {"grade": grade, "count": 0}}))
        for other in (("origin-b", *b[1:]), (*b[:3], "window-2")):
            self.assertIsNone(total((a, b), {a: verified, other: verified}))
        self.assertIsNone(total((a, b), {a: verified}))
        self.assertIsNone(total((), {}))
        self.assertIsNone(total((a,), {a: None}))
        self.assertEqual(total((a, a), {a: verified}), 2)
        self.assertIsNone(total((a,), {a: {"grade": "verified", "count": True}}))

    def test_published_gate_keeps_credential_failures_out_of_digests(self):
        guide = (Path(__file__).resolve().parents[1] / "docs" / "PYTHON-READER.md").read_text()
        section = guide.split("## Caller example: keep credential failure out of an empty digest\n", 1)[1]
        code = section.split("```python\n", 1)[1].split("\n```", 1)[0]
        namespace = {}
        exec(compile(code, "docs/PYTHON-READER.md", "exec"), namespace)
        guarded_pull = namespace["guarded_pull"]

        empty = {"httpStatus": 200, "hasMore": False, "processed": 0}
        nonempty = {"httpStatus": 200, "hasMore": False, "processed": 1}

        def committed(*, token="synthetic-current-token", credential_id="credential-1",
                      generation="g1", attempt_id="attempt-1", state="committed"):
            return {"attemptId": attempt_id, "state": state, "credentialId": credential_id,
                    "generation": generation, "token": token}

        current = committed()
        old_committed = committed(token="synthetic-old-token", credential_id="credential-old",
                                  generation="g-old")
        mutable_current = committed()
        cases = (
            # name, wrapper state, wrapper token, authority records, page, result, calls, selected token
            ("refresh_exception", "raises", "synthetic-old-token", (), empty, "indeterminate",
             ("attempt", "acquire"), None),
            ("unavailable", "unavailable", "synthetic-old-token", (current,), empty, "indeterminate",
             ("attempt", "acquire"), None),
            ("stale_with_age", "stale-with-age", "synthetic-old-token", (current,), empty, "indeterminate",
             ("attempt", "acquire"), None),
            ("caught_failure_claims_fresh", "fresh", "synthetic-old-token", (None,), empty, "indeterminate",
             ("attempt", "acquire", "authority"), None),
            ("half_committed_refresh", "fresh", "synthetic-old-token",
             (committed(generation="g2", token="synthetic-new-token", state="pending"),),
             empty, "indeterminate", ("attempt", "acquire", "authority"), None),
            ("wrong_attempt", "fresh", "synthetic-old-token",
             (committed(attempt_id="another-attempt"),), empty, "indeterminate",
             ("attempt", "acquire", "authority"), None),
            # A swallowed refresh can propose an old token as fresh. Its committed
            # attempt selection is evaluated by independent manager eligibility.
            ("swallowed_refresh_fallback_revoked", "fresh", "synthetic-old-token",
             (old_committed,), empty, "indeterminate",
             ("attempt", "acquire", "authority"), None),
            ("swallowed_refresh_fallback_valid_reuse", "fresh", "synthetic-old-token",
             (old_committed, old_committed), empty, "observed_empty_unverified",
             ("attempt", "acquire", "authority", "read", "authority"),
             "synthetic-old-token"),
            ("revoked_before_read", "reused", "synthetic-current-token",
             (current,), empty, "indeterminate", ("attempt", "acquire", "authority"), None),
            ("revoked_after_read", "reused", "synthetic-current-token",
             (current, current), nonempty, "indeterminate",
             ("attempt", "acquire", "authority", "read", "authority"),
             "synthetic-current-token"),
            ("eligibility_wrong_identity", "reused", "synthetic-current-token",
             (current,), empty, "indeterminate", ("attempt", "acquire", "authority"), None),
            ("eligibility_missing_time", "reused", "synthetic-current-token",
             (current,), empty, "indeterminate", ("attempt", "acquire", "authority"), None),
            ("eligibility_noncanonical_time", "reused", "synthetic-current-token",
             (current,), empty, "indeterminate", ("attempt", "acquire", "authority"), None),
            ("bare_string_eligibility", "reused", "synthetic-current-token",
             (current,), empty, "indeterminate", ("attempt", "acquire", "authority"), None),
            ("selection_changed_after_read", "fresh", "synthetic-old-token",
             (current, committed(credential_id="credential-2", generation="g2")),
             empty, "indeterminate", ("attempt", "acquire", "authority", "read", "authority"),
             "synthetic-current-token"),
            ("same_ids_token_changed_after_read", "fresh", "synthetic-current-token",
             (current, committed(token="synthetic-replaced-token")),
             nonempty, "indeterminate", ("attempt", "acquire", "authority", "read", "authority"),
             "synthetic-current-token"),
            ("mutable_selected_record", "fresh", "synthetic-current-token",
             (mutable_current, mutable_current), nonempty, "indeterminate",
             ("attempt", "acquire", "authority", "read", "authority"),
             "synthetic-current-token"),
            ("missing_receipt", "fresh", "synthetic-current-token", (current, current),
             None, "indeterminate", ("attempt", "acquire", "authority", "read", "authority"),
             "synthetic-current-token"),
            ("raw_http_401", "fresh", "synthetic-current-token", (current, current),
             {**empty, "httpStatus": 401}, "indeterminate",
             ("attempt", "acquire", "authority", "read", "authority"),
             "synthetic-current-token"),
            ("raw_http_403", "fresh", "synthetic-current-token", (current, current),
             {**empty, "httpStatus": 403}, "indeterminate",
             ("attempt", "acquire", "authority", "read", "authority"),
             "synthetic-current-token"),
            ("reader_rejects_401", "fresh", "synthetic-current-token", (current,),
             None, "indeterminate", ("attempt", "acquire", "authority", "read"),
             "synthetic-current-token"),
            ("reader_rejects_403", "fresh", "synthetic-current-token", (current,),
             None, "indeterminate", ("attempt", "acquire", "authority", "read"),
             "synthetic-current-token"),
            ("fresh_empty", "fresh", "synthetic-current-token", (current, current),
             empty, "observed_empty_unverified",
             ("attempt", "acquire", "authority", "read", "authority"), "synthetic-current-token"),
            ("old_last_known_empty", "reused", "synthetic-current-token", (current, current),
             empty, "observed_empty_unverified",
             ("attempt", "acquire", "authority", "read", "authority"),
             "synthetic-current-token"),
            ("sink_mutates_copy", "fresh", "synthetic-current-token", (current, current),
             nonempty, "nonempty_written",
             ("attempt", "acquire", "authority", "read", "authority", "digest"),
             "synthetic-current-token"),
            ("sink_failure_after_read", "fresh", "synthetic-current-token", (current, current),
             nonempty, "indeterminate",
             ("attempt", "acquire", "authority", "read", "authority"),
             "synthetic-current-token"),
            ("misleading_old_token", "fresh", "synthetic-old-token",
             (committed(token="synthetic-new-token", generation="g2"),
              committed(token="synthetic-new-token", generation="g2")),
             empty, "observed_empty_unverified",
             ("attempt", "acquire", "authority", "read", "authority"), "synthetic-new-token"),
            ("fresh_incomplete", "fresh", "synthetic-current-token", (current, current),
             {**empty, "hasMore": True}, "incomplete",
             ("attempt", "acquire", "authority", "read", "authority"), "synthetic-current-token"),
            ("reused_credential_nonempty", "reused", "synthetic-current-token", (current, current),
             nonempty, "nonempty_written",
             ("attempt", "acquire", "authority", "read", "authority", "digest"),
             "synthetic-current-token"),
            ("fresh_nonempty", "fresh", "synthetic-current-token", (current, current),
             nonempty, "nonempty_written",
             ("attempt", "acquire", "authority", "read", "authority", "digest"),
             "synthetic-current-token"),
        )
        for name, state, claimed_token, authority_records, receipt, expected, expected_calls, selected_token in cases:
            with self.subTest(name=name):
                calls = []
                remaining = list(authority_records)
                eligibility_returns = list({
                    "swallowed_refresh_fallback_revoked": ("revoked",),
                    "revoked_before_read": ("revoked",),
                    "revoked_after_read": ("eligible", "revoked"),
                }.get(name, ("eligible", "eligible")))
                eligibility_calls = []
                recorded_observations = []
                caught_refresh_failures = []

                def new_attempt_id():
                    calls.append("attempt")
                    return "attempt-1"

                def acquire(attempt_id):
                    calls.append("acquire")
                    self.assertEqual(attempt_id, "attempt-1")
                    if state == "raises":
                        raise RuntimeError("synthetic late refresh failure")
                    if name.startswith("swallowed_refresh_fallback_"):
                        try:
                            raise RuntimeError("synthetic refresh failed before fallback")
                        except RuntimeError as error:
                            caught_refresh_failures.append(str(error))
                        # Deliberately misleading wrapper proposal; the independent
                        # committed attempt and eligibility decide whether to read.
                    return {"state": state, "generation": "g2", "token": claimed_token}

                def load_committed_attempt(attempt_id):
                    calls.append("authority")
                    self.assertEqual(attempt_id, "attempt-1")
                    return remaining.pop(0)

                def load_credential_eligibility(credential_id, generation):
                    eligibility_calls.append((credential_id, generation))
                    self.assertEqual((credential_id, generation),
                                     (authority_records[0]["credentialId"],
                                      authority_records[0]["generation"]))
                    state = eligibility_returns.pop(0)
                    if name == "bare_string_eligibility":
                        return state
                    observation = {"credentialId": credential_id, "generation": generation,
                                   "state": state,
                                   "checkedAtUtc": ("2024-01-01T00:00:00Z" if name == "old_last_known_empty"
                                                    else "2026-10-15T22:00:00Z")}
                    if name == "eligibility_wrong_identity":
                        observation["credentialId"] = "credential-2"
                    if name == "eligibility_missing_time":
                        observation.pop("checkedAtUtc")
                    if name == "eligibility_noncanonical_time":
                        observation["checkedAtUtc"] = "2026-1-5T22:00:00Z"
                    if name == "old_last_known_empty":
                        observation["token"] = "synthetic-private-token"
                        observation["private"] = {"secret": "synthetic-private-metadata"}
                    return observation

                def record_eligibility_observation(attempt_id, phase, observation):
                    self.assertEqual(attempt_id, "attempt-1")
                    self.assertEqual(phase, "before" if not recorded_observations else "after")
                    self.assertEqual(set(observation),
                                     {"credentialId", "generation", "state", "checkedAtUtc"})
                    self.assertNotIn("token", observation)
                    if name == "sink_failure_after_read" and phase == "after":
                        raise OSError("synthetic private record write failed")
                    recorded_observations.append((phase, dict(observation)))
                    if name == "sink_mutates_copy":
                        observation["state"] = "revoked"

                def read_once(token):
                    calls.append("read")
                    self.assertEqual(token, selected_token)
                    if name in {"reader_rejects_401", "reader_rejects_403"}:
                        raise reader.ReaderError("http_error", status=int(name[-3:]))
                    if name == "mutable_selected_record":
                        mutable_current["token"] = "synthetic-replaced-token"
                    return receipt

                def write_digest(value):
                    calls.append("digest")
                    self.assertIs(value, receipt)
                    self.assertGreater(value["processed"], 0)

                self.assertEqual(guarded_pull(new_attempt_id, acquire, load_committed_attempt,
                                              load_credential_eligibility,
                                              record_eligibility_observation, read_once,
                                              write_digest), expected)
                self.assertEqual(tuple(calls), expected_calls)
                self.assertEqual(len(caught_refresh_failures),
                                 1 if name.startswith("swallowed_refresh_fallback_") else 0)
                self.assertEqual(len(remaining), len(authority_records) - expected_calls.count("authority"))
                one_check = {"swallowed_refresh_fallback_revoked", "revoked_before_read",
                             "eligibility_wrong_identity",
                             "eligibility_missing_time", "eligibility_noncanonical_time",
                             "bare_string_eligibility",
                             "selection_changed_after_read",
                             "same_ids_token_changed_after_read", "mutable_selected_record",
                             "reader_rejects_401", "reader_rejects_403"}
                expected_eligibility_checks = (1 if name in one_check else
                                               2 if "read" in expected_calls else 0)
                self.assertEqual(len(eligibility_calls), expected_eligibility_checks)
                invalid_observation = {"eligibility_wrong_identity", "eligibility_missing_time",
                                       "eligibility_noncanonical_time", "bare_string_eligibility"}
                self.assertEqual(len(recorded_observations),
                                 0 if name in invalid_observation else
                                 expected_eligibility_checks - (name == "sink_failure_after_read"))
                if name == "revoked_after_read":
                    self.assertEqual([record["state"] for _, record in recorded_observations],
                                     ["eligible", "revoked"])
                if name == "swallowed_refresh_fallback_revoked":
                    self.assertEqual([record["state"] for _, record in recorded_observations],
                                     ["revoked"])
                if name == "swallowed_refresh_fallback_valid_reuse":
                    self.assertEqual([record["state"] for _, record in recorded_observations],
                                     ["eligible", "eligible"])
                if name == "old_last_known_empty":
                    self.assertEqual([record["checkedAtUtc"] for _, record in recorded_observations],
                                     ["2024-01-01T00:00:00Z"] * 2)

        # The current credential row can rotate while this attempt's committed
        # selection remains stable. Both observations still use the old token.
        for receipt, expected, digest_count in ((nonempty, "nonempty_written", 1),
                                                (empty, "observed_empty_unverified", 0)):
            with self.subTest(rotation_result=expected):
                calls = []
                by_attempt = {"attempt-1": committed()}
                current_row = committed(token="synthetic-new-token",
                                        credential_id="credential-2", generation="g2")

                def acquire(attempt_id):
                    calls.append("acquire")
                    self.assertEqual(attempt_id, "attempt-1")
                    return {"state": "reused"}

                def load_committed_attempt(attempt_id):
                    calls.append("authority")
                    return dict(by_attempt[attempt_id])

                def load_credential_eligibility(credential_id, generation):
                    calls.append("eligibility")
                    self.assertEqual((credential_id, generation), ("credential-1", "g1"))
                    # The old selection remains locally eligible despite pointer rotation.
                    return {"credentialId": credential_id, "generation": generation,
                            "state": "eligible", "checkedAtUtc": "2026-10-15T22:00:00Z"}

                def record_eligibility_observation(attempt_id, phase, observation):
                    calls.append("record_" + phase)
                    self.assertEqual(attempt_id, "attempt-1")
                    self.assertEqual((observation["credentialId"], observation["generation"]),
                                     ("credential-1", "g1"))
                    self.assertNotIn("token", observation)

                def read_once(token):
                    calls.append("read")
                    self.assertEqual(token, "synthetic-current-token")
                    current_row.update(generation="g3", token="synthetic-rotated-token")
                    return receipt

                def write_digest(value):
                    calls.append("digest")
                    self.assertIs(value, receipt)

                result = guarded_pull(lambda: "attempt-1", acquire,
                                      load_committed_attempt, load_credential_eligibility,
                                      record_eligibility_observation, read_once,
                                      write_digest)
                self.assertEqual(result, expected)
                self.assertEqual(current_row["generation"], "g3")
                self.assertEqual(current_row["token"], "synthetic-rotated-token")
                self.assertEqual(calls, ["acquire", "authority", "eligibility",
                                         "record_before", "read", "authority", "eligibility",
                                         "record_after"]
                                 + ["digest"] * digest_count)


if __name__ == "__main__":
    unittest.main()
