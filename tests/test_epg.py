import gzip
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlsplit

from scripts import epg


XML = b'<tv><channel id="demo"><display-name>Demo</display-name></channel><programme channel="demo" start="20261025120000 +0800"><title>Show</title></programme></tv>'
PLAYLIST = '#EXTM3U\n## url-tvg="https://example.invalid/epg"\n#EXTINF:-1 tvg-id="demo",Demo\nhttps://example.invalid/live\n'


class Response:
    def __init__(self, body=b"", error=None, status_error=None, close_error=None):
        self.content = body
        self.error = error
        self.status_error = status_error
        self.close_error = close_error
        self.close_count = 0

    def raise_for_status(self):
        if self.status_error:
            raise self.status_error

    def iter_content(self, chunk_size):
        yield self.content
        if self.error:
            raise self.error

    def close(self):
        self.close_count += 1
        if self.close_error:
            raise self.close_error


class EpgTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="epg_tests_")
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.output = self.base / "merged.xml.gz"
        self.notes = self.base / "notes.md"
        self.config = patch.multiple(
            epg,
            M3U_URL="https://example.invalid/list",
            OUTPUT_PATH=str(self.output),
            RELEASE_LOG=str(self.notes),
            CF_PROXY_URL="",
            CHECK_LOGOS=False,
            RETRIES=1,
            MIN_OK_RATIO=0.75,
            HAS_CURL_CFFI=True,
        )
        self.config.start()
        self.addCleanup(self.config.stop)

    def fixture(self, body):
        path = self.base / "source.xml"
        path.write_bytes(body)
        return path

    def run_main(self, body, playlist=PLAYLIST):
        responses = []

        def get(url, **kwargs):
            response = Response(playlist.encode() if url == epg.M3U_URL else body)
            responses.append(response)
            return response

        with patch.object(epg, "cf_requests", SimpleNamespace(get=get)):
            result = epg.main()
        self.assertTrue(all(response.close_count == 1 for response in responses))
        return result

    def test_original_custom_source_parsing_is_preserved(self):
        playlist = PLAYLIST.replace(
            'url-tvg="https://example.invalid/epg"',
            'url-tvg="https://example.invalid/a,https://example.invalid/b" x-tvg-url="https://example.invalid/a"',
        ) + '## x-gary-epg-url="https://example.invalid/c"\n'
        urls, ids, logos = epg.parse_m3u(playlist)
        self.assertEqual(urls, [f"https://example.invalid/{name}" for name in "abc"])
        self.assertEqual(ids, {"demo"})
        self.assertEqual(logos, [])
        for attribute in ("url-tvg", "x-tvg-url", "x-gary-epg-url"):
            with self.subTest(attribute=attribute):
                self.assertEqual(epg.parse_m3u(PLAYLIST.replace("url-tvg", attribute))[0], ["https://example.invalid/epg"])
        standard_header = PLAYLIST.replace('#EXTM3U\n## ', '#EXTM3U ')
        self.assertEqual(epg.parse_m3u(standard_header)[0], [])

    def test_proxy_accepts_endpoint_prefix_and_authentication_query(self):
        target = "https://example.invalid/epg?key=a/b&lang=zh#guide"
        for proxy in (
            "https://proxy.invalid/proxy",
            "https://proxy.invalid/proxy?url=",
            "https://proxy.invalid/proxy?auth=private&url=old",
        ):
            with self.subTest(proxy=proxy), patch.object(epg, "CF_PROXY_URL", proxy):
                query = parse_qs(urlsplit(epg._proxied(target)).query)
                self.assertEqual(query["url"], [target])
                if "auth=" in proxy:
                    self.assertEqual(query["auth"], ["private"])
        self.assertEqual(epg._proxied(target), target)

    def test_invalid_root_and_empty_source_are_rejected(self):
        for body in (b"<html><body>Maintenance</body></html>", b"<tv/>"):
            with self.subTest(body=body), self.assertRaises(epg.EpgValidationError):
                epg.parse_epg_file(self.fixture(body), "https://example.invalid/epg", {"demo"})

    def test_invalid_or_unmatched_guides_preserve_existing_output(self):
        for body in (
            b"<html><body>Maintenance</body></html>",
            b"<tv/>",
            XML.replace(b"demo", b"other"),
            b'<tv><channel id="demo"><display-name>Demo</display-name></channel></tv>',
            b'<tv><channel id="demo"/><programme channel="other" start="20261025120000 +0800"/></tv>',
        ):
            with self.subTest(body=body):
                self.output.write_bytes(b"previous-good-release")
                self.assertEqual(self.run_main(body), 1)
                self.assertEqual(self.output.read_bytes(), b"previous-good-release")

    def test_missing_ids_stop_before_source_downloads(self):
        playlist = PLAYLIST.replace('tvg-id="demo"', 'tvg-id=""')
        with patch.object(epg, "fetch_text", return_value=playlist), patch.object(epg, "download_epg") as download:
            self.assertEqual(epg.main(), 1)
            download.assert_not_called()

    def test_plain_and_gzip_guides_complete_full_pipeline(self):
        for body in (XML, gzip.compress(XML)):
            with self.subTest(compressed=body != XML):
                self.assertEqual(self.run_main(body), 0)
                root = epg.ET.fromstring(gzip.decompress(self.output.read_bytes()))
                self.assertEqual(root.find("channel/display-name").text, "Demo")
                self.assertEqual(root.find("programme/title").text, "Show")

    def test_partial_recovery_and_utc_deduplication(self):
        result = epg.parse_epg_file(self.fixture(XML[:-5]), "https://example.invalid/epg", {"demo"})
        self.assertTrue(result.partial)
        other = epg.ET.fromstring('<tv><programme channel="demo" start="20261025040000 +0000"><title>Duplicate</title></programme></tv>')
        merged = epg.merge_epg([result.root, other], {"demo"})
        self.assertEqual(len(merged.findall("programme")), 1)
        self.assertEqual(merged.find("programme/title").text, "Show")

    def test_source_success_ratio_still_blocks_degraded_guides(self):
        urls = [f"https://example.invalid/{index}" for index in range(4)]
        playlist = PLAYLIST.replace("https://example.invalid/epg", ",".join(urls))
        for complete in (2, 3):
            results = [
                epg.EpgDownloadResult(url, epg.ET.fromstring(XML), partial=index >= complete)
                for index, url in enumerate(urls)
            ]
            with self.subTest(complete=complete), patch.object(epg, "fetch_text", return_value=playlist), patch.object(
                epg, "download_epg", side_effect=lambda url, ids: results[urls.index(url)]
            ):
                self.assertEqual(epg.main(), 0 if complete == 3 else 1)

    def test_full_urls_and_error_details_are_preserved(self):
        url = "https://review_user:review_password@example.invalid/private_path?token=review_token#review_fragment"
        encoded = quote(url, safe="")
        result = epg.EpgDownloadResult(url, epg.ET.fromstring(XML))
        epg.write_epg_source_notes([result])
        epg.release_note(f"Failure: {url}; encoded target {encoded}")
        record = logging.LogRecord("review", logging.ERROR, "", 0, "Failed %s", (url,), None)
        rendered = logging.Formatter("%(message)s").format(record)
        public = self.notes.read_text() + rendered + epg.error_detail(RuntimeError(f"token=review_token {url}"))
        self.assertIn(url, self.notes.read_text())
        self.assertIn(encoded, self.notes.read_text())
        self.assertIn(url, rendered)
        self.assertIn(url, epg.error_detail(RuntimeError(url)))
        for private in ("review_user", "review_password", "private_path", "review_token", "review_fragment"):
            self.assertIn(private, public)

    def test_m3u_failure_returns_nonzero_with_full_error_text(self):
        secret = "review_token"
        with patch.object(epg, "fetch_text", side_effect=RuntimeError(secret)), self.assertLogs(epg.log, level="ERROR") as captured:
            self.assertEqual(epg.main(), 1)
        self.assertIn(secret, "\n".join(captured.output))

    def test_source_urls_with_separators_are_preserved_verbatim(self):
        for suffix in ("?token=prefix|review_token", "/prefix'review_token", "/prefix`review_token"):
            with self.subTest(suffix=suffix):
                self.notes.unlink(missing_ok=True)
                url = "https://example.invalid" + suffix
                epg.write_epg_source_notes([epg.EpgDownloadResult(url, epg.ET.fromstring(XML))])
                self.assertIn(url, self.notes.read_text())

    def test_stream_failure_cleans_files_and_closes_response(self):
        for response in (
            Response(XML, error=OSError("interrupted stream")),
            Response(XML, status_error=RuntimeError("HTTP failure")),
            Response(XML, close_error=OSError("close failure")),
            Response(XML, error=KeyboardInterrupt()),
        ):
            with self.subTest(response=response), patch.object(epg.tempfile, "tempdir", str(self.base)), patch.object(
                epg, "cf_requests", SimpleNamespace(get=lambda *args, **kwargs: response)
            ):
                with self.assertRaises(BaseException):
                    epg.fetch_to_file("https://example.invalid/epg")
                self.assertEqual(response.close_count, 1)
                self.assertEqual(list(self.base.glob("epg_*.xml")), [])

    def test_m3u_http_errors_also_close_response(self):
        response = Response(status_error=RuntimeError("HTTP failure"))
        with patch.object(epg, "cf_requests", SimpleNamespace(get=lambda *args, **kwargs: response)):
            with self.assertRaises(RuntimeError):
                epg.fetch_bytes("https://example.invalid/list")
        self.assertEqual(response.close_count, 1)

    def test_logo_budget_kills_stalled_process_after_output_is_ready(self):
        actual_run = subprocess.run
        checked = []

        def stalled_process(command, **kwargs):
            checked.append(self.output.exists())
            return actual_run([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)

        playlist = PLAYLIST.replace('tvg-id="demo"', 'tvg-id="demo" tvg-logo="https://example.invalid/logo"')
        started = time.monotonic()
        with patch.object(epg, "CHECK_LOGOS", True), patch.object(epg, "LOGO_CHECK_TIMEOUT", 0.1), patch.object(
            epg.subprocess, "run", side_effect=stalled_process
        ):
            self.assertEqual(self.run_main(XML, playlist), 0)
        self.assertEqual(checked, [True])
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn("Time budget exceeded", self.notes.read_text())

    def test_logo_worker_notes_are_appended_only_on_success(self):
        def completed(command, **kwargs):
            Path(kwargs["env"]["RELEASE_LOG"]).write_text("## Logo Check\n1/1 OK\n")
            return subprocess.CompletedProcess(command, 0)

        epg.release_note("Existing merge summary")
        with patch.object(epg.subprocess, "run", side_effect=completed):
            epg.check_tvg_logos_bounded([("Demo", "https://example.invalid/logo")])
        self.assertIn("Existing merge summary", self.notes.read_text())
        self.assertIn("1/1 OK", self.notes.read_text())

    def test_logo_worker_failure_is_nonfatal(self):
        with patch.object(epg.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "worker")):
            epg.check_tvg_logos_bounded([("Demo", "https://example.invalid/logo")])
        self.assertIn("EPG generation succeeded", self.notes.read_text())


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="epg_release_tests_")
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        (self.base / "output").mkdir()
        (self.base / "output/release_notes.md").write_text("Test release")
        (self.base / "output/merged_epg.xml.gz").write_bytes(gzip.compress(XML))
        self.calls = self.base / "calls"
        self.state = self.base / "published"
        self.state.write_text("previous-good-release")
        self.stub = self.base / "bash_env"
        self.stub.write_text(
            'gh() {\n'
            '  printf "%s\\n" "$*" >> "$GH_CALLS"\n'
            '  if [ "${GH_FAIL:-}" = "$2" ]; then return 9; fi\n'
            '  if [ "$2" = edit ]; then printf "%s" "$3" > "$GH_STATE"; fi\n'
            '}\n'
        )
        self.script = Path(__file__).resolve().parents[1] / "scripts/publish_release.sh"

    def publish(self, failure="", attempt="1"):
        env = dict(
            os.environ,
            BASH_ENV=str(self.stub),
            GH_CALLS=str(self.calls),
            GH_STATE=str(self.state),
            GH_FAIL=failure,
            GITHUB_REPOSITORY="review/example",
            GITHUB_SHA="a" * 40,
            GITHUB_RUN_ID="100",
            GITHUB_RUN_ATTEMPT=attempt,
            MERGE_TIME="2026-10-25T16:00:00+0800",
        )
        return subprocess.run(["bash", str(self.script)], cwd=self.base, env=env, capture_output=True, text=True)

    def test_success_uploads_draft_before_switching_latest(self):
        result = self.publish(attempt="2")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text().splitlines()
        self.assertEqual([call.split()[1] for call in calls], ["create", "upload", "edit"])
        self.assertIn("--draft", calls[0])
        self.assertNotIn("--latest", calls[0])
        self.assertIn("--draft=false --latest", calls[2])
        self.assertEqual(self.state.read_text(), "epg-100-2")

    def test_create_and_upload_failures_preserve_previous_release(self):
        for failure, expected in (("create", ["create"]), ("upload", ["create", "upload"])):
            with self.subTest(failure=failure):
                self.calls.unlink(missing_ok=True)
                result = self.publish(failure=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual([call.split()[1] for call in self.calls.read_text().splitlines()], expected)
                self.assertEqual(self.state.read_text(), "previous-good-release")

    def test_corrupt_output_prevents_any_release_operation(self):
        (self.base / "output/merged_epg.xml.gz").write_bytes(b"broken gzip")
        self.assertNotEqual(self.publish().returncode, 0)
        self.assertFalse(self.calls.exists())
        self.assertEqual(self.state.read_text(), "previous-good-release")


if __name__ == "__main__":
    unittest.main()
