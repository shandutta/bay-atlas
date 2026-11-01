import gzip
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from datetime import date
from unittest.mock import patch

from server import AtlasServer, DataStore


class FakeRefresh:
    def __init__(self):
        self.started = 0

    def status(self):
        return {"status": "idle", "running": False}

    def start(self):
        self.started += 1
        return {"status": "running", "running": True}

    def provider_status(self):
        return {"configured": True}

    def configure(self, payload):
        return {"configured": bool(payload.get("api_key"))}


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.refresh = FakeRefresh()
        cls.server = AtlasServer(("127.0.0.1", 0), data_path=Path(cls.temp.name) / "missing.json", refresh_manager=cls.refresh)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.host = "127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        status, payload = response.status, json.loads(response.read())
        conn.close()
        return status, payload

    def test_missing_data_is_explicit_and_never_fabricated(self):
        status, payload = self.request("GET", "/api/data")
        self.assertEqual(status, 200)
        self.assertEqual(payload["sales"], [])
        self.assertEqual(payload["metadata"]["status"], "awaiting_data")
        self.assertNotIn("rows", payload)

    def test_data_summaries_never_expose_private_import_or_coverage_reviews(self):
        path = Path(self.temp.name) / "private-import-summary.json"
        private = "synthetic-private-review-marker"
        path.write_text(json.dumps({"sales": [], "last_import": {
            "counts": {"total": 12, "eligible": 3, "added": 2, "excluded_quarantined": 1,
                       "updated": private, "stale": True, "conflicts": -1, "private_details": private},
            "imported_at": "2026-10-31T12:00:00Z", "refresh_mode": "manual_csv", "coverage_complete": False,
            "source": private, "export_filename": private, "export_sha256": private,
            "market_sale_quarantine": [{"address": private, "source_id": private,
                "evidence_file": private, "evidence_sha256": private, "raw_file": private, "raw_sha256": private}],
            "review": {"decision": private, "reason": private}}, "coverage": {
            "label": "County searches", "description": "Observed sales", "note": "Partial coverage",
            "complete": False, "refresh_mode": "automatic_api", "counties_completed": 9, "counties_total": 9,
            "county_searches_finished": True, "review": {"evidence_file": private}, "regions": {"private": private},
            "verified_source_gaps": [{"county": "Example County, CA", "unreachable_count": 12,
                "source_coverage": "partial", "run_id": private, "raw_file": private,
                "review": {"market_sale_quarantine": private}}]}}))
        with patch.object(self.server, "store", DataStore(path)):
            status, payload = self.request("GET", "/api/data")
        self.assertEqual(status, 200)
        self.assertEqual(payload["metadata"]["last_import"], {
            "counts": {"total": 12, "eligible": 3, "added": 2, "excluded_quarantined": 1},
            "imported_at": "2026-10-31T12:00:00+00:00", "refresh_mode": "manual_csv", "coverage_complete": False})
        self.assertEqual(payload["coverage"], payload["metadata"]["coverage"])
        self.assertEqual(payload["coverage"]["verified_source_gaps"], [
            {"county": "Example County, CA", "unreachable_count": 12, "source_coverage": "partial"}])
        self.assertEqual(payload["coverage"]["counties_completed"], 9)
        self.assertEqual(payload["coverage"]["description"], "Observed sales")
        rendered = json.dumps(payload)
        for forbidden in (private, "market_sale_quarantine", "evidence_file", "evidence_sha256", "raw_sha256"):
            self.assertNotIn(forbidden, rendered)

    def test_malformed_summary_fields_cannot_pass_private_text(self):
        from server import public_import_summary, public_coverage_summary
        self.assertIsNone(public_import_summary(["private"]))
        self.assertEqual(public_import_summary({"counts": "private", "refresh_mode": "private",
                                               "imported_at": "private", "coverage_complete": "private"}), {})
        self.assertEqual(public_coverage_summary({"counties_completed": "private", "verified_source_gaps": [
            None, {"county": ["private"]}, {"county": "Example", "unreachable_count": "private"}]}),
            {"verified_source_gaps": [{"county": "Example"}]})

    def test_gzip_data_matches_plain_json_and_head_length(self):
        def fetch(method, encoding):
            conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
            conn.request(method, "/api/data", headers={"Accept-Encoding": encoding})
            response = conn.getresponse()
            result = response.status, dict(response.getheaders()), response.read()
            conn.close()
            return result

        status, plain_headers, plain = fetch("GET", "gzip;q=0, *;q=1")
        self.assertEqual(status, 200)
        self.assertNotIn("Content-Encoding", plain_headers)
        status, headers, compressed = fetch("GET", "br, gzip;q=0.5")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Encoding"], "gzip")
        self.assertEqual(headers["Vary"], "Accept-Encoding")
        self.assertEqual(int(headers["Content-Length"]), len(compressed))
        self.assertLess(len(compressed), len(plain))
        self.assertEqual(gzip.decompress(compressed), plain)
        self.assertNotIn("rows", json.loads(plain))
        _, head_headers, head = fetch("HEAD", "gzip")
        self.assertEqual(head, b"")
        self.assertEqual(head_headers["Content-Encoding"], "gzip")
        self.assertEqual(int(head_headers["Content-Length"]), len(compressed))

    def test_direct_homes_prefix_and_invalid_input(self):
        status, _ = self.request("GET", "/homes/api/data")
        self.assertEqual(status, 200)
        status, _ = self.request("GET", "/api/estimate?lat=nan&lng=-122.25")
        self.assertEqual(status, 400)
        status, _ = self.request("GET", "/api/geocode?q=Oakland")
        self.assertEqual(status, 400)

    def test_traversal_cannot_expose_backend_or_provider_files(self):
        for path in ("/%2e%2e/server.py", "/homes/../server.py", "/data/provider.json"):
            with self.subTest(path=path):
                status, _ = self.request("GET", path)
                self.assertEqual(status, 404)

    def test_refresh_requires_same_origin_and_json(self):
        for headers in ({"Content-Type": "application/json"},
                        {"Origin": "https://other.example", "Content-Type": "application/json"},
                        {"Origin": "http://" + self.host, "Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"}):
            status, _ = self.request("POST", "/api/refresh", body="{}", headers=headers)
            self.assertEqual(status, 403)
        status, _ = self.request("POST", "/api/refresh", body="{}", headers={"Origin": "http://" + self.host})
        self.assertEqual(status, 415)
        status, payload = self.request("POST", "/api/refresh", body="{}", headers={"Origin": "http://" + self.host, "Content-Type": "application/json"})
        self.assertEqual(status, 202)
        self.assertTrue(payload["running"])

    def test_provider_configuration_does_not_echo_key(self):
        status, payload = self.request("POST", "/api/provider", body='{"api_key":"secret-test-value"}',
                                       headers={"Origin": "http://" + self.host, "Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertNotIn("secret-test-value", json.dumps(payload))

    def test_unrecognized_host_cannot_read_or_write(self):
        headers = {"Host": "attacker.example", "Origin": "https://attacker.example", "Content-Type": "application/json"}
        for method, path in (("GET", "/api/data"), ("POST", "/api/provider")):
            status, _ = self.request(method, path, body="{}" if method == "POST" else None, headers=headers)
            self.assertEqual(status, 403)

    def test_malformed_or_ambiguous_authority_is_rejected(self):
        for host in ("localhost:invalid", "localhost:99999", "localhost:0", "localhost?anything",
                     "localhost#anything", "user@localhost", "localhost/", "local host"):
            with self.subTest(host=host):
                status, _ = self.request("GET", "/api/data", headers={"Host": host})
                self.assertEqual(status, 403)

    def test_unsupported_chunked_request_cannot_start_refresh(self):
        before = self.refresh.started
        status, _ = self.request("POST", "/api/refresh", body="0\r\n\r\n", headers={
            "Origin": "http://" + self.host, "Content-Type": "application/json", "Transfer-Encoding": "chunked"})
        self.assertEqual(status, 400)
        self.assertEqual(self.refresh.started, before)

    def test_duplicate_host_and_length_headers_are_rejected(self):
        for duplicate, expected in (("Host", 403), ("Content-Length", 400)):
            with self.subTest(duplicate=duplicate):
                conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
                conn.putrequest("POST", "/api/refresh", skip_host=True)
                conn.putheader("Host", self.host)
                conn.putheader("Origin", "http://" + self.host)
                conn.putheader("Content-Type", "application/json")
                conn.putheader("Content-Length", "2")
                conn.putheader(duplicate, self.host if duplicate == "Host" else "2")
                conn.endheaders(b"{}")
                response = conn.getresponse()
                self.assertEqual(response.status, expected)
                response.read()
                conn.close()

    def test_homes_redirect_adds_trailing_slash(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        conn.request("GET", "/homes")
        response = conn.getresponse()
        self.assertEqual(response.status, 308)
        self.assertEqual(response.getheader("Location"), "/homes/")
        response.read()
        conn.close()

    def test_unchanged_static_asset_revalidates_without_resending_body(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        conn.request("GET", "/")
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        etag = response.getheader("ETag")
        self.assertTrue(etag)
        self.assertTrue(response.read())
        conn.request("GET", "/", headers={"If-None-Match": etag})
        response = conn.getresponse()
        self.assertEqual(response.status, 304)
        self.assertEqual(response.getheader("ETag"), etag)
        self.assertEqual(response.read(), b"")
        conn.close()

    def test_missing_or_broken_data_file_is_not_ready(self):
        path = Path(self.temp.name) / "broken.json"
        path.write_text("not JSON")
        payload = DataStore(path).snapshot()
        self.assertEqual(payload["status"], "data_error")
        self.assertEqual(payload["sales"], [])

    def test_static_gzip_negotiation_head_revalidation_and_changed_file(self):
        root = Path(self.temp.name) / "static-gzip"
        root.mkdir(exist_ok=True)
        asset = root / "boundary.json"
        original = json.dumps({"coordinates": [1, 2, 3] * 1000}).encode()
        asset.write_bytes(original)

        def fetch(method="GET", encoding="gzip", etag=None):
            headers = {"Accept-Encoding": encoding}
            if etag:
                headers["If-None-Match"] = etag
            conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
            conn.request(method, "/boundary.json", headers=headers)
            response = conn.getresponse()
            result = response.status, dict(response.getheaders()), response.read()
            conn.close()
            return result

        with patch("server.PUBLIC_DIR", root):
            status, headers, compressed = fetch()
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Encoding"], "gzip")
            self.assertEqual(headers["Vary"], "Accept-Encoding")
            self.assertEqual(gzip.decompress(compressed), original)
            self.assertLess(len(compressed), len(original))
            with patch("server.gzip.compress", side_effect=AssertionError("Reuse compressed static bytes")):
                _, head, body = fetch("HEAD")
                self.assertEqual(body, b"")
                self.assertEqual(int(head["Content-Length"]), len(compressed))
            _, plain_headers, plain = fetch(encoding="gzip;q=0, *;q=1")
            self.assertNotIn("Content-Encoding", plain_headers)
            self.assertEqual(plain, original)
            status, revalidated, body = fetch(etag=headers["ETag"])
            self.assertEqual(status, 304)
            self.assertEqual(revalidated["Vary"], "Accept-Encoding")
            self.assertEqual(body, b"")
            changed = original + b" "
            asset.write_bytes(changed)
            status, updated, body = fetch(etag=headers["ETag"])
            self.assertEqual(status, 200)
            self.assertNotEqual(updated["ETag"], headers["ETag"])
            self.assertEqual(gzip.decompress(body), changed)

    def test_nonobject_saved_json_is_a_data_error_not_a_server_crash(self):
        path = Path(self.temp.name) / "invalid-schema.json"
        for invalid in ([], None, 123, "text"):
            with self.subTest(invalid=invalid):
                path.write_text(json.dumps(invalid))
                payload = DataStore(path).snapshot()
                self.assertEqual(payload["status"], "data_error")
                self.assertEqual(payload["sales"], [])

    def test_serialized_data_is_reused_and_invalidated_by_file_or_day(self):
        path = Path(self.temp.name) / "response-cache.json"
        store = DataStore(path)
        with patch("server.today_utc", return_value=date(2026, 1, 1)):
            first, compressed = store.serialized(gzip_ok=True)
            self.assertTrue(compressed)
            with patch("server.gzip.compress", side_effect=AssertionError("unchanged data must reuse gzip")):
                self.assertIs(store.serialized(gzip_ok=True)[0], first)
            plain, compressed = store.serialized()
            self.assertFalse(compressed)
            self.assertEqual(gzip.decompress(first), plain)
            self.assertNotIn("rows", json.loads(plain))
            path.write_text(json.dumps({"sales": [], "coverage": {"label": "Updated", "complete": False}}))
            changed, _ = store.serialized()
            self.assertIsNot(changed, plain)
            self.assertEqual(json.loads(changed)["coverage"]["label"], "Updated")
        with patch("server.today_utc", return_value=date(2026, 1, 2)):
            next_day, _ = store.serialized()
            self.assertEqual(json.loads(next_day)["metadata"]["as_of"], "2026-01-02")
            self.assertNotEqual(changed, next_day)


if __name__ == "__main__":
    unittest.main()
