#!/usr/bin/env python3
"""
1. Parse all EPG source URLs (url-tvg / x-tvg-url) and channel tvg-ids from an M3U URL.
2. Download all EPG XMLs concurrently (supports gzip / deflate / plain).
3. Merge XMLs, keeping only <channel> and <programme> nodes whose ids match
   the tvg-ids found in the M3U.
4. Write the result to OUTPUT_PATH as a gzip-compressed file (default: output/merged_epg.xml.gz).

Environment variables:
    M3U_URL         Required. M3U playlist URL (Cloudflare-protected sites supported).
    OUTPUT_PATH     Optional. Output file path (default: output/merged_epg.xml.gz).
    MAX_WORKERS     Optional. Concurrent download threads (default: 6).
    REQUEST_TIMEOUT Optional. Per-request timeout in seconds (default: 30).
    CF_PROXY_URL    Optional. Cloudflare Workers reverse-proxy prefix.
                    Example: https://epg-proxy.your-name.workers.dev/proxy
    IMPERSONATE     Optional. Browser fingerprint to impersonate (default: "chrome").
                    Use a bare alias to always track the latest fingerprint:
                      chrome        latest Chrome (recommended)
                      safari        latest Safari
                      safari_ios    latest Mobile Safari
                    Or pin to a specific version (must be supported by installed curl-cffi):
                      chrome136  chrome131  chrome124
                      edge146    safari17_0
"""

from __future__ import annotations

import certifi
import gzip
import json
import logging
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import zlib

from bisect import bisect_right

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

# root ca for *.samsungcloud.tv
ROOT_PEM = """-----BEGIN CERTIFICATE-----
MIIDrzCCApegAwIBAgIQCDvgVpBCRrGhdWrJWZHHSjANBgkqhkiG9w0BAQUFADBh
MQswCQYDVQQGEwJVUzEVMBMGA1UEChMMRGlnaUNlcnQgSW5jMRkwFwYDVQQLExB3
d3cuZGlnaWNlcnQuY29tMSAwHgYDVQQDExdEaWdpQ2VydCBHbG9iYWwgUm9vdCBD
QTAeFw0wNjExMTAwMDAwMDBaFw0zMTExMTAwMDAwMDBaMGExCzAJBgNVBAYTAlVT
MRUwEwYDVQQKEwxEaWdpQ2VydCBJbmMxGTAXBgNVBAsTEHd3dy5kaWdpY2VydC5j
b20xIDAeBgNVBAMTF0RpZ2lDZXJ0IEdsb2JhbCBSb290IENBMIIBIjANBgkqhkiG
9w0BAQEFAAOCAQ8AMIIBCgKCAQEA4jvhEXLeqKTTo1eqUKKPC3eQyaKl7hLOllsB
CSDMAZOnTjC3U/dDxGkAV53ijSLdhwZAAIEJzs4bg7/fzTtxRuLWZscFs3YnFo97
nh6Vfe63SKMI2tavegw5BmV/Sl0fvBf4q77uKNd0f3p4mVmFaG5cIzJLv07A6Fpt
43C/dxC//AH2hdmoRBBYMql1GNXRor5H4idq9Joz+EkIYIvUX7Q6hL+hqkpMfT7P
T19sdl6gSzeRntwi5m3OFBqOasv+zbMUZBfHWymeMr/y7vrTC0LUq7dBMtoM1O/4
gdW7jVg/tRvoSSiicNoxBN33shbyTApOB6jtSj1etX+jkMOvJwIDAQABo2MwYTAO
BgNVHQ8BAf8EBAMCAYYwDwYDVR0TAQH/BAUwAwEB/zAdBgNVHQ4EFgQUA95QNVbR
TLtm8KPiGxvDl7I90VUwHwYDVR0jBBgwFoAUA95QNVbRTLtm8KPiGxvDl7I90VUw
DQYJKoZIhvcNAQEFBQADggEBAMucN6pIExIK+t1EnE9SsPTfrgT1eXkIoyQY/Esr
hMAtudXH/vTBH1jLuG2cenTnmCmrEbXjcKChzUyImZOMkXDiqw8cvpOp/2PV5Adg
06O/nVsJ8dWO41P0jmP6P6fbtGbfYmbW0W5BjfIttep3Sp+dWOIrWcBAI+0tKIJF
PnlUkiaY4IBIqDfv8NZ5YBberOgOzW6sRBc4L0na4UU+Krk2U886UAb3LujEV0ls
YSEY1QSteDwsOoBrp+uvFRTp2InBuThs4pFsiv9kuXclVzDAGySj4dzp30d8tbQk
CAUw7C29C79Fv1C5qfPrmAESrciIxpg0X40KPMbp1ZWVbd4=
-----END CERTIFICATE-----"""

BUNDLE_PATH = "/tmp/epg-merge-filter-ca-bundle.pem"
with open(certifi.where()) as f:
    base = f.read()
with open(BUNDLE_PATH, "w") as f:
    f.write(base + "\n" + ROOT_PEM)

# curl_cffi: mimics real browser TLS/HTTP2 fingerprints to bypass Cloudflare
try:
    from curl_cffi import requests as cf_requests

    HAS_CURL_CFFI = True
except ImportError:
    try:
        import requests as cf_requests  # fallback; may be blocked by Cloudflare
    except ImportError:
        cf_requests = None
    HAS_CURL_CFFI = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

M3U_URL = os.environ.get("M3U_URL", "")
OUTPUT_PATH = os.environ.get("OUTPUT_PATH", "output/merged_epg.xml.gz")
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "6"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "30"))
CF_PROXY_URL = os.environ.get("CF_PROXY_URL", "")
IMPERSONATE = os.environ.get("IMPERSONATE", "chrome")
RELEASE_LOG = os.environ.get("RELEASE_LOG", "output/release_notes.md")
RETRIES = int(os.environ.get("RETRIES", "3"))  # attempts per download
MIN_OK_RATIO = float(os.environ.get("MIN_OK_RATIO", "0.75"))  # below this, do not publish
CHECK_LOGOS = os.environ.get("CHECK_LOGOS", "1") != "0"
LOGO_CHECK_TIMEOUT = float(os.environ.get("LOGO_CHECK_TIMEOUT", "30"))
LOGO_REQUEST_TIMEOUT = min(REQUEST_TIMEOUT, float(os.environ.get("LOGO_REQUEST_TIMEOUT", "5")))
PREFERRED_EPG_HOST = os.environ.get("PREFERRED_EPG_HOST", "epgshare01.online").lower()
NOTES_MAX_CHARS = 120_000  # GitHub release body limit is 125,000

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


class EpgValidationError(ValueError):
    pass


def error_detail(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _proxied(url: str) -> str:
    """Wrap the target URL through the Workers reverse proxy if configured."""
    if CF_PROXY_URL:
        parts = urlsplit(CF_PROXY_URL)
        query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "url"]
        query.append(("url", url))
        return urlunsplit(parts._replace(query=urlencode(query)))
    return url


def with_retry(fn, *args, what: str = ""):
    """Call fn(*args), retrying transient failures with exponential backoff + jitter."""
    for attempt in range(1, RETRIES + 1):
        try:
            return fn(*args)
        except Exception as exc:
            if attempt == RETRIES:
                raise
            delay = 2**attempt + random.random()
            log.warning(f"  retry {attempt}/{RETRIES - 1} [{what}]: {error_detail(exc)} (sleep {delay:.0f}s)")
            time.sleep(delay)


def fetch_bytes(url: str) -> bytes:
    """
    Download a URL and return raw (decompressed) bytes.
    Uses curl_cffi to mimic Chrome TLS + HTTP/2 fingerprints,
    bypassing Cloudflare Bot Management without a real browser.
    Falls back to plain requests if curl_cffi is unavailable.
    """
    target = _proxied(url)
    kwargs = dict(timeout=REQUEST_TIMEOUT)

    if cf_requests is None:
        raise RuntimeError("Install curl_cffi or requests to fetch remote EPG sources")

    if HAS_CURL_CFFI:
        resp = cf_requests.get(target, 
            headers={
                "User-Agent": "AptvPlayer/1.5.4",
                "Accept": "*/*",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
            },
            impersonate=IMPERSONATE, **kwargs)
    else:
        log.warning(
            "curl_cffi not installed; falling back to requests (may be blocked by Cloudflare)"
        )
        resp = cf_requests.get(
            target,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Encoding": "gzip, deflate, br",
            },
            **kwargs,
        )

    try:
        resp.raise_for_status()
        data = resp.content
    finally:
        resp.close()
    # Some servers declare Content-Encoding but skip actual decompression
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def fetch_to_file(url: str) -> Path:
    """Download a URL to a temporary file without buffering the body in memory."""
    target = _proxied(url)
    kwargs = dict(timeout=REQUEST_TIMEOUT)

    if cf_requests is None:
        raise RuntimeError("Install curl_cffi or requests to fetch remote EPG sources")

    headers = {
        "User-Agent": "AptvPlayer/1.5.4",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }

    if HAS_CURL_CFFI:
        resp = cf_requests.get(
            target, headers=headers, impersonate=IMPERSONATE, stream=True, **kwargs
        )
    else:
        log.warning(
            "curl_cffi not installed; falling back to requests (may be blocked by Cloudflare)"
        )
        resp = cf_requests.get(target, headers=headers, stream=True, **kwargs)

    tmp_path = None
    try:
        with closing(resp):
            resp.raise_for_status()
            with tempfile.NamedTemporaryFile(prefix="epg_", suffix=".xml", delete=False) as tmp:
                tmp_path = Path(tmp.name)
                if hasattr(resp, "iter_content"):
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            tmp.write(chunk)
                else:
                    tmp.write(resp.content)
    except BaseException:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


def fetch_text(url: str) -> str:
    data = with_retry(fetch_bytes, url, what="M3U")
    try:
        return data.decode("utf-8").lstrip("\ufeff")
    except UnicodeDecodeError:
        return data.decode("latin-1")


# ---------------------------------------------------------------------------
# M3U parsing
# ---------------------------------------------------------------------------


def parse_m3u(text: str) -> tuple[list[str], set[str], list[tuple[str, str]]]:
    """
    Parse M3U text and return:
      epg_urls  : deduplicated, order-preserving list of EPG XML URLs
      tvg_ids   : set of all channel tvg-ids
      tvg_logos : list of (display_name, logo_url) for every entry that has a tvg-logo
    """
    epg_urls: list[str] = []
    tvg_ids: set[str] = set()
    seen_urls: set[str] = set()
    tvg_names: list[str] = []
    tvg_ids_duplicate: list[str] = []
    tvg_id_count = 0
    tvg_logos: list[tuple[str, str]] = []

    for line in text.splitlines():
        line = line.strip()

        # EPG URLs may appear in url-tvg or x-tvg-url attributes,
        # and may be comma-separated lists
        if line.startswith("##"):
            for attr in ("url-tvg", "x-tvg-url", "x-gary-epg-url"):
                m = re.search(rf'{attr}="([^"]*)"', line, re.IGNORECASE)
                if m:
                    for raw in m.group(1).split(","):
                        url = raw.strip()
                        if url and url not in seen_urls:
                            epg_urls.append(url)
                            seen_urls.add(url)

        m = re.search(r'tvg-id="([^"]*)"', line, re.IGNORECASE)
        if m:
            tvg_id_count = tvg_id_count + 1
            tid = m.group(1).strip()
            if tid:
                if tid in tvg_ids:
                    tvg_ids_duplicate.append(tid)
                else:
                    tvg_ids.add(tid)
            else:
                m = re.search(r'tvg-name="([^"]*)"', line, re.IGNORECASE)
                if m:
                    tname = m.group(1).strip()
                    if tname:
                        tvg_names.append(tname)

        logo_m = re.search(r'tvg-logo="([^"]*)"', line, re.IGNORECASE)
        if logo_m:
            logo_url = logo_m.group(1).strip()
            if logo_url:
                name_m = re.search(r'tvg-name="([^"]*)"', line, re.IGNORECASE)
                display_name = name_m.group(1).strip() if name_m else ""
                if not display_name:
                    id_m = re.search(r'tvg-id="([^"]*)"', line, re.IGNORECASE)
                    display_name = id_m.group(1).strip() if id_m else ""
                if not display_name:
                    display_name = line.rsplit(",", 1)[-1].strip()
                tvg_logos.append((display_name or logo_url, logo_url))

    channel_summary = [
        "## Channels",
        "### Summary",
        "| Total | Invalid | Duplicated | Unique |",
        "|--------|--------|--------|------:|",
        f"| {tvg_id_count} | {len(tvg_names)} | {len(tvg_ids_duplicate)} | {len(tvg_ids)} |",
    ]

    release_note("\n".join(channel_summary))

    if tvg_names:
        release_note(f"### Invalid IDs ({len(tvg_names)})")
        release_note("> Entries where `tvg-id` is empty or whitespace-only.")
        for tvg_name in sorted(tvg_names):
            release_note(f"- {tvg_name}")

    if tvg_ids_duplicate:
        release_note(f"### Duplicated IDs ({len(tvg_ids_duplicate)})")
        release_note(
            "> The same `tvg-id` appears on more than one M3U entry. First occurrence is kept."
        )
        for tvg_id in sorted(tvg_ids_duplicate):
            release_note(f"- {tvg_id}")

    log.info(
        f"Found {len(epg_urls)} EPG source(s), {tvg_id_count} tvg-id(s), {len(tvg_names)} invalid tvg-id(s), {len(tvg_ids_duplicate)} duplicated tvg-id(s), {len(tvg_ids)} unique tvg-id(s), {len(tvg_logos)} tvg-logo(s)"
    )

    return epg_urls, tvg_ids, tvg_logos


# ---------------------------------------------------------------------------
# tvg-logo availability check
# ---------------------------------------------------------------------------


def check_logo_url(url: str) -> tuple[bool, str]:
    target = _proxied(url)
    headers = {
        "User-Agent": "AptvPlayer/1.5.4",
        "Accept": "*/*",
    }
    resp = None
    try:
        if HAS_CURL_CFFI:
            resp = cf_requests.get(
                target,
                headers=headers,
                impersonate=IMPERSONATE,
                timeout=LOGO_REQUEST_TIMEOUT,
                verify=BUNDLE_PATH,
                stream=True,  # headers only; body is never read
            )
        else:
            resp = cf_requests.get(
                target, headers=headers, timeout=LOGO_REQUEST_TIMEOUT, verify=BUNDLE_PATH,
                stream=True,
            )
        status = resp.status_code
        return status < 400, str(status)
    except Exception as exc:
        return False, error_detail(exc)
    finally:
        if resp is not None and hasattr(resp, "close"):
            resp.close()


def check_tvg_logos(tvg_logos: list[tuple[str, str]]) -> None:
    if not tvg_logos:
        return

    url_to_names: dict[str, list[str]] = {}
    for name, url in tvg_logos:
        url_to_names.setdefault(url, []).append(name)

    failed: list[tuple[str, list[str], str]] = []
    checked = 0

    log.info(f"Checking {len(url_to_names)} tvg-logo URL(s)")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(check_logo_url, url): url for url in url_to_names}
        for fut in as_completed(futures):
            url = futures[fut]
            ok, detail = fut.result()
            checked += 1
            if not ok:
                failed.append((url, url_to_names[url], detail))
                log.warning(f"  FAILED [{url}]: {detail}")

    log.info(f"Logo check complete: {checked - len(failed)}/{checked} OK")

    logo_summary = [
        "## Logo Check",
        f"### Summary ({checked - len(failed)}/{checked} OK)",
    ]
    release_note("\n".join(logo_summary))

    if failed:
        release_note(f"### Failed Logos ({len(failed)})")
        release_note("> Entries where tvg-logo is unavailable.")
        for url, names, detail in sorted(failed, key=lambda item: item[0]):
            names_str = ";".join(sorted(set(names)))
            release_note(f"- {names_str}: {url} ({detail})")


def check_tvg_logos_bounded(tvg_logos: list[tuple[str, str]]) -> None:
    if not tvg_logos or LOGO_CHECK_TIMEOUT <= 0:
        return
    with tempfile.TemporaryDirectory(prefix="epg_logos_") as directory:
        input_path = Path(directory) / "logos.json"
        notes_path = Path(directory) / "logos.md"
        input_path.write_text(json.dumps(tvg_logos), encoding="utf-8")
        env = dict(os.environ, RELEASE_LOG=str(notes_path))
        try:
            subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--check-logos", str(input_path)],
                env=env,
                timeout=LOGO_CHECK_TIMEOUT,
                check=True,
            )
        except subprocess.TimeoutExpired:
            log.warning("Logo check exceeded its time budget; continuing with the merged EPG")
            release_note("## Logo Check\n> Time budget exceeded; logo availability was not fully checked.")
        except (OSError, subprocess.CalledProcessError) as exc:
            log.warning(f"Logo check could not complete: {error_detail(exc)}")
            release_note("## Logo Check\n> Logo availability check failed; EPG generation succeeded.")
        else:
            if notes_path.exists():
                release_note(notes_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# EPG download & merge
# ---------------------------------------------------------------------------


@dataclass
class EpgDownloadResult:
    url: str
    root: ET.Element | None = None
    channels: int = 0
    programmes: int = 0
    error: str | None = None
    partial: bool = False

    @property
    def ok(self) -> bool:
        return self.root is not None


def is_gzip_file(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(2) == b"\x1f\x8b"


def parse_epg_file(path: Path, url: str, tvg_ids: set[str]) -> EpgDownloadResult:
    """Stream-parse one XMLTV file and keep only relevant channel/programme nodes."""
    filtered = ET.Element("tv")
    channels = 0
    programmes = 0
    source_root: ET.Element | None = None

    partial_error: str | None = None
    try:
        input_file = gzip.open(path, "rb") if is_gzip_file(path) else path.open("rb")
        with input_file as source:
            for event, elem in ET.iterparse(source, events=("start", "end")):
                if event == "start" and source_root is None:
                    if elem.tag != "tv":
                        raise EpgValidationError("EPG root element must be <tv>")
                    source_root = elem
                    for attr, val in elem.attrib.items():
                        filtered.set(attr, val)
                    continue

                if event != "end":
                    continue

                if elem.tag == "channel":
                    channels += 1
                    cid = elem.get("id", "").strip()
                    if cid in tvg_ids:
                        filtered.append(deepcopy(elem))
                    elem.clear()
                    if source_root is not None:
                        source_root.clear()
                elif elem.tag == "programme":
                    programmes += 1
                    channel = elem.get("channel", "").strip()
                    if channel in tvg_ids:
                        filtered.append(deepcopy(elem))
                    elem.clear()
                    if source_root is not None:
                        source_root.clear()
    except (ET.ParseError, EOFError, OSError, zlib.error) as exc:
        # Truncated download or a stray bad byte: keep everything parsed so far.
        partial_error = f"{type(exc).__name__}: {exc}"

    if partial_error:
        if channels + programmes == 0:
            raise EpgValidationError(partial_error)
        return EpgDownloadResult(
            url, filtered, channels, programmes, error=partial_error, partial=True
        )
    if channels + programmes == 0:
        raise EpgValidationError("EPG contains no channels or programmes")
    return EpgDownloadResult(url, filtered, channels, programmes)


def download_epg(url: str, tvg_ids: set[str]) -> EpgDownloadResult:
    """
    Download and stream-parse one EPG XML, retrying transient failures.
    A truncated/corrupt download counts as a failed attempt; if no attempt is
    complete, the most complete partial result is returned instead of nothing.
    """
    best: EpgDownloadResult | None = None
    last_error = "unknown error"
    for attempt in range(1, RETRIES + 1):
        tmp_path: Path | None = None
        try:
            log.info(f"Downloading EPG ({attempt}/{RETRIES}): {url}")
            tmp_path = fetch_to_file(url)
            result = parse_epg_file(tmp_path, url, tvg_ids)
            if not result.partial:
                log.info(
                    f"  OK {url} — channels: {result.channels}  programmes: {result.programmes}"
                )
                return result
            last_error = result.error or last_error
            if best is None or result.programmes > best.programmes:
                best = result
            log.warning(f"  PARTIAL [{url}]: {last_error}")
        except Exception as exc:
            last_error = error_detail(exc)
            log.warning(f"  FAILED [{url}] attempt {attempt}: {last_error}")
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        if attempt < RETRIES:
            time.sleep(2**attempt + random.random())
    return best if best is not None else EpgDownloadResult(url, error=last_error)


def write_epg_source_notes(results: list[EpgDownloadResult]) -> None:
    ok_count = sum(1 for result in results if result.ok and not result.partial)
    epg_source_summary = [
        "## EPG Sources",
        f"### Summary ({ok_count}/{len(results)} OK)",
        "| URL | Status | Channels | Programmes |",
        "|--------|--------|--------|------:|",
    ]
    release_note("\n".join(epg_source_summary))

    for result in results:
        if result.partial:
            release_note(
                f"| {result.url} | PARTIAL: {result.error} | {result.channels} | {result.programmes} |"
            )
        elif result.ok:
            release_note(
                f"| {result.url} | OK | {result.channels} | {result.programmes} |"
            )
        else:
            release_note(f"| {result.url} | {result.error} | - | - |")


_XMLTV_TS = re.compile(
    r"^(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?\s*([+-]\d{4})?$"
)


def to_utc(ts: str) -> str:
    """Normalise an XMLTV timestamp to UTC 'YYYYMMDDhhmmss'; unparsable input is returned as-is."""
    m = _XMLTV_TS.match(ts.strip())
    if not m:
        return ts
    y, mo, d, h, mi, s, tz = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0))
    except ValueError:
        return ts
    if tz:
        sign = -1 if tz[0] == "-" else 1
        dt -= timedelta(minutes=sign * (int(tz[1:3]) * 60 + int(tz[3:5])))
    return dt.strftime("%Y%m%d%H%M%S")


def programme_key(prog: ET.Element) -> tuple[str, str]:
    """A channel cannot start two programmes at the same instant, so (channel, UTC start)
    is the identity. Sources that disagree on the stop time or time-zone notation
    no longer produce duplicate/overlapping entries; the first source wins."""
    channel = prog.get("channel", "").strip()
    start = to_utc(prog.get("start", "").strip())
    if channel and start:
        return channel, start
    return channel, ET.tostring(prog, encoding="unicode")


def programme_span(prog: ET.Element) -> tuple[str, str] | None:
    start = to_utc(prog.get("start", "").strip())
    stop = to_utc(prog.get("stop", "").strip())
    if len(start) != 14 or not start.isdigit():
        return None
    if len(stop) != 14 or not stop.isdigit() or stop <= start:
        stop = start
    return start, stop


def overlaps(spans: list[tuple[str, str]], span: tuple[str, str]) -> bool:
    start, stop = span
    index = bisect_right(spans, (start, "\uffff"))
    if index and (spans[index - 1][1] > start or spans[index - 1][0] == start):
        return True
    return index < len(spans) and spans[index][0] < stop


def add_spans(spans: list[tuple[str, str]], new: list[tuple[str, str]]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for start, stop in sorted(spans + new):
        if result and start < result[-1][1]:
            if stop > result[-1][1]:
                result[-1] = (result[-1][0], stop)
        else:
            result.append((start, stop))
    return result


def is_preferred_source(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return bool(PREFERRED_EPG_HOST) and (host == PREFERRED_EPG_HOST or host.endswith("." + PREFERRED_EPG_HOST))


class CountingWriter:
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.bytes_written = 0

    def write(self, data: bytes) -> int:
        self.bytes_written += len(data)
        return self.wrapped.write(data)

    def flush(self) -> None:
        self.wrapped.flush()


def merge_epg(roots: list[ET.Element], tvg_ids: set[str]) -> ET.Element:
    """
    Merge multiple <tv> root elements with deduplication and filtering:
      <channel id="X">        kept only when X is in tvg_ids
      <programme channel="X"> kept only when X is in tvg_ids
    """
    merged = ET.Element("tv")

    now = datetime.now(timezone.utc)
    merged.set("date", now.strftime("%Y%m%d%H%M%S %z"))
    merged.set("source-info-name", "epgshare01")
    merged.set("source-info-url", "https://epgshare01.online/")
    merged.set("source-data-url", "https://epgshare01.online/epgshare01/")
    merged.set("generator-info-name", "epg-merge-filter")
    merged.set("generator-info-url", "https://github.com/iwinstar/epg_merge_filter")

    channel_elems: list[ET.Element] = []
    programme_elems: list[ET.Element] = []

    seen_channels: set[str] = set()
    seen_programmes: set[tuple[str, str, str] | tuple[str, str]] = set()
    programme_channels: set[str] = set()
    coverage: dict[str, list[tuple[str, str]]] = {}
    total_prog = 0
    duplicate_prog = 0
    overlap_prog = 0

    for root in roots:
        # Carry over root-level attributes from the first source that defines them
        # for attr, val in root.attrib.items():
        #     if attr not in merged.attrib:
        #         merged.set(attr, val)

        for ch in root.findall("channel"):
            cid = ch.get("id", "").strip()
            if cid in tvg_ids and cid not in seen_channels:
                seen_icon = set()

                for icon in list(ch.findall("icon")):
                    src = icon.get("src")
                    if not src or src in seen_icon:
                        ch.remove(icon)
                    else:
                        seen_icon.add(src)

                channel_elems.append(ch)
                seen_channels.add(cid)

        added: dict[str, list[tuple[str, str]]] = {}
        for prog in root.findall("programme"):
            channel = prog.get("channel", "").strip()
            if channel not in tvg_ids:
                continue

            key = programme_key(prog)
            if key in seen_programmes:
                duplicate_prog += 1
                continue

            span = programme_span(prog)
            if span is not None and overlaps(coverage.get(channel, []), span):
                overlap_prog += 1
                continue

            programme_elems.append(prog)
            seen_programmes.add(key)
            programme_channels.add(channel)
            if span is not None:
                added.setdefault(channel, []).append(span)
            total_prog += 1

        for channel, spans in added.items():
            coverage[channel] = add_spans(coverage.get(channel, []), spans)

    for ch in channel_elems:
        merged.append(ch)
    for prog in programme_elems:
        merged.append(prog)

    log.info(
        f"Merge complete — channels kept: {len(seen_channels)}/{len(tvg_ids)}, "
        f"programme entries: {total_prog}, duplicate programmes skipped: {duplicate_prog}, "
        f"overlapping programmes skipped: {overlap_prog}"
    )

    unmatched = tvg_ids - seen_channels
    channels_without_programmes = seen_channels - programme_channels

    merge_summary = [
        "## Merge Result",
        "### Summary",
        "| Channels | Unmatched | Matched | No Programme | Programmes | Duplicated Programmes | Overlapping Programmes |",
        "|--------|--------|--------|--------|--------|--------|------:|",
        f"| {len(tvg_ids)} | {len(unmatched)} | {len(seen_channels)} | {len(channels_without_programmes)} | {total_prog} | {duplicate_prog} | {overlap_prog} |",
    ]
    release_note("\n".join(merge_summary))

    if unmatched:
        release_note(f"### Unmatched IDs ({len(unmatched)})")
        release_note("> Entries where `tvg-id` not found in any EPG source")
        for uid in sorted(unmatched):
            release_note(f"- {uid}")

    if channels_without_programmes:
        release_note(
            f"### Channels Without Programmes ({len(channels_without_programmes)})"
        )
        release_note("> Matched `<channel>` entries that have no merged `<programme>`.")
        for cid in sorted(channels_without_programmes):
            release_note(f"- {cid}")

    return merged


# ---------------------------------------------------------------------------
# Icon URL upgrade
# ---------------------------------------------------------------------------

ICON_URL_REPLACEMENTS: list[tuple[str, str]] = [
    ("focus.telerama.fr/252x168", "focus.telerama.fr/1764x1176"),
    ("thumb.canalplus.pro/http/unsafe/256x143", "thumb.canalplus.pro/http/unsafe/1792x1001"),
    ("thumb.canalplus.pro/http/unsafe/640x360", "thumb.canalplus.pro/http/unsafe/1920x1080")
]


def upgrade_icon_urls(root: ET.Element) -> int:
    upgraded = 0
    for icon in root.findall(".//programme/icon"):
        src = icon.get("src", "")
        if not src:
            continue
        new_src = src
        for old, new in ICON_URL_REPLACEMENTS:
            new_src = new_src.replace(old, new)
        if new_src != src:
            icon.set("src", new_src)
            upgraded += 1
    return upgraded


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def write_xml(root: ET.Element, path: str) -> None:
    """
    Serialize the merged XML tree and write it as a gzip-compressed file.
    Appends .gz to path automatically if not already present.
    """
    if not path.endswith(".gz"):
        path = path + ".gz"

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    ET.indent(root, space="  ")
    tree = ET.ElementTree(root)

    with gzip.open(path, "wb", compresslevel=9) as gz:
        counter = CountingWriter(gz)
        counter.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        counter.write(b'<!DOCTYPE tv SYSTEM "xmltv.dtd">\n')
        counter.write(b'<!-- Generated with EPG Merge Filter -->\n')
        tree.write(counter, encoding="utf-8", xml_declaration=False)

    raw_kb = counter.bytes_written / 1024
    gz_kb = os.path.getsize(path) / 1024
    ratio = (1 - gz_kb / raw_kb) * 100 if raw_kb else 0
    log.info(
        f"Written to {path} ({gz_kb:.1f} KB compressed, {raw_kb:.1f} KB raw, {ratio:.0f}% reduction)"
    )

    file_path = Path(path)
    file_summary = [
        "### File Summary",
        "| Name | Raw Size | Compressed Size | Reduction |",
        "|--------|--------|--------|------:|",
        f"| {file_path.name} | {raw_kb:.1f} KB | {gz_kb:.1f} KB | {ratio:.0f}% |",
    ]
    release_note("\n".join(file_summary))


# ---------------------------------------------------------------------------
# Record release notes
# ---------------------------------------------------------------------------


def release_note(content: str, path: str | None = None):
    file_path = Path(path or RELEASE_LOG)
    if file_path.suffix != ".md":
        file_path = file_path.with_suffix(".md")

    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("a", encoding="utf-8") as f:
        f.write(content + "\n")


def cap_release_notes(path: str | None = None, limit: int = NOTES_MAX_CHARS) -> None:
    """GitHub rejects release bodies over 125,000 chars; trim at a line boundary."""
    file_path = Path(path or RELEASE_LOG)
    if file_path.suffix != ".md":
        file_path = file_path.with_suffix(".md")
    if not file_path.exists():
        return
    text = file_path.read_text(encoding="utf-8")
    if len(text) <= limit:
        return
    cut = text.rfind("\n", 0, limit)
    file_path.write_text(
        text[: cut if cut > 0 else limit] + "\n\n> … truncated (release body size limit).\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    if not M3U_URL:
        log.error("M3U_URL environment variable is not set")
        return 1

    Path(RELEASE_LOG).unlink(missing_ok=True)  # do not accumulate notes across local runs

    if not HAS_CURL_CFFI:
        log.warning("Install curl_cffi for Cloudflare bypass: pip install curl_cffi")

    log.info(f"Fetching M3U: {M3U_URL}")
    try:
        m3u_text = fetch_text(M3U_URL)
    except Exception as exc:
        log.error(f"Could not fetch M3U: {error_detail(exc)}")
        return 1
    epg_urls, tvg_ids, tvg_logos = parse_m3u(m3u_text)

    if not epg_urls:
        log.error("No EPG URLs found in M3U (url-tvg / x-tvg-url)")
        return 1

    if not tvg_ids:
        log.error("No non-empty tvg-id values found in M3U")
        return 1

    results_by_url: dict[str, EpgDownloadResult] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(download_epg, url, tvg_ids): url for url in epg_urls}
        for fut in as_completed(futures):
            url = futures[fut]
            results_by_url[url] = fut.result()

    results = [results_by_url[url] for url in epg_urls]
    write_epg_source_notes(results)

    fully_ok = sum(1 for r in results if r.ok and not r.partial)
    if fully_ok / len(results) < MIN_OK_RATIO:
        # Non-zero exit makes the later workflow steps skip, so the last good release stays online.
        log.error(f"Only {fully_ok}/{len(results)} EPG sources fully OK (< {MIN_OK_RATIO:.0%}); not publishing a degraded EPG")
        return 1

    ordered = sorted(results, key=lambda result: not is_preferred_source(result.url))
    roots = [result.root for result in ordered if result.root is not None]

    if not roots:
        log.error("All EPG downloads failed")
        return 1

    merged = merge_epg(roots, tvg_ids)

    channel_ids = {channel.get("id", "").strip() for channel in merged.findall("channel")}
    if not channel_ids or not any(
        programme.get("channel", "").strip() in channel_ids
        for programme in merged.findall("programme")
    ):
        message = "Merged EPG has no matched channels with programmes; not publishing"
        log.error(message)
        release_note(f"## Publication Blocked\n> {message}")
        return 1

    upgraded = upgrade_icon_urls(merged)
    log.info(f"Icon URLs upgraded: {upgraded}")

    write_xml(merged, OUTPUT_PATH)
    if CHECK_LOGOS:
        check_tvg_logos_bounded(tvg_logos)
    cap_release_notes()
    return 0


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--check-logos":
            check_tvg_logos(json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")))
            sys.exit(0)
        sys.exit(main())
    except Exception as exc:
        log.error(f"EPG processing failed: {error_detail(exc)}")
        sys.exit(1)
