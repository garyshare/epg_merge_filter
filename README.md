# EPG Merge Filter

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.x-blue.svg)](https://www.python.org/)

Fetches EPG (XMLTV) sources declared in an M3U playlist, merges them into a single deduplicated XML file, and publishes the result as a gzip-compressed GitHub Release asset — all automatically via GitHub Actions.

## How it works

1. **Parse M3U** — reads `url-tvg` / `tvg-id` attributes from an M3U playlist supplied via the `M3U_URL` secret (or a manual override).
2. **Fetch EPG sources concurrently** — downloads each XMLTV URL in parallel (configurable worker count and timeout) using [`curl-cffi`](https://github.com/lexiforest/curl_cffi), which replicates a real browser's TLS/HTTP2 fingerprint to bypass Cloudflare Bot Management without a headless browser.
3. **Merge & deduplicate** — combines `<channel>` and `<programme>` elements across all sources, dropping duplicates keyed on channel ID.
4. **Validate & publish** — refuses to publish an empty guide, then uploads `output/merged_epg.xml.gz` to a new draft Release. Only a successful upload is published and marked as latest, preserving the previous download when an upload fails. After publishing, every other release (including drafts left by failed uploads) and every other tag is deleted, so only one release and one tag exist at any time.

## Download

The latest merged EPG is always available at:

```
https://github.com/garyshare/epg_merge_filter/releases/latest/download/merged_epg.xml.gz
```

## Configuration

All runtime parameters are controlled through environment variables / GitHub secrets and Actions inputs — no code changes needed.

| Variable | Where to set | Description |
|---|---|---|
| `M3U_URL` | Repository secret | URL of the source M3U playlist |
| `CF_PROXY_URL` | Repository secret | *(Optional)* Workers endpoint, e.g. `https://epg-proxy.example.workers.dev/proxy`. An existing `?url=` prefix or authentication query parameters are supported. |
| `MAX_WORKERS` | Workflow env | Parallel fetch threads (default `6`) |
| `REQUEST_TIMEOUT` | Workflow env | Per-request timeout in seconds (default `60`) |
| `CHECK_LOGOS` | Environment | Set to `0` to skip optional logo checks (default `1`) |
| `LOGO_CHECK_TIMEOUT` | Workflow env | Total logo-check budget in seconds (default `30`); expiry does not block EPG publication |
| `LOGO_REQUEST_TIMEOUT` | Workflow env | Per-logo request timeout in seconds (default `5`) |
| `IMPERSONATE` | Workflow input / env | Browser fingerprint for curl-cffi: `chrome`, `safari`, `safari_ios`, `chrome136`, … (default `chrome`) |
| `OUTPUT_PATH` | Workflow env | Output file path (default `output/merged_epg.xml.gz`) |

The original M3U parsing rules are preserved: EPG source attributes are read from custom `##` lines. URLs and exception messages are emitted in full in logs and release notes. Publication requires at least 75% of sources to parse completely and at least one matched channel with a programme.

## Triggering a run

The workflow (`Merge EPG`) can be triggered three ways:

- **Push** — automatically on `main` commits that modify `scripts/epg.py`, `scripts/publish_release.sh`, or `.github/workflows/epg.yml`.
- **Schedule** — runs every 4 hours at minute 17 (UTC 00:17, 04:17, … 20:17), approximately 180 scheduled runs per 30-day month.
- **Manual** — use *Actions → Merge EPG → Run workflow*, optionally overriding `m3u_url` and `impersonate`.

## Requirements

- Python ≥ 3.10 (workflow uses 3.14)
- Install dependencies with `pip install -r requirements.txt` (`curl-cffi >= 0.16.3, < 0.17`).
- GitHub CLI (`gh`) for publishing; included on GitHub-hosted Ubuntu runners.

Run local regression checks with `python -B -m unittest discover -s tests -v`. Tests use local fixtures and simulated network and release operations.

## License

Apache 2.0
