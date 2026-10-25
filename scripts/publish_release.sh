set -euo pipefail

: "${GITHUB_REPOSITORY:?}"
: "${GITHUB_SHA:?}"
: "${GITHUB_RUN_ID:?}"
: "${GITHUB_RUN_ATTEMPT:?}"
: "${MERGE_TIME:?}"

test -s output/release_notes.md
gzip -t output/merged_epg.xml.gz

release_tag="epg-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"

gh release create "$release_tag" \
  --repo "$GITHUB_REPOSITORY" \
  --target "$GITHUB_SHA" \
  --title "Merged EPG @ ${MERGE_TIME}" \
  --notes-file output/release_notes.md \
  --draft

gh release upload "$release_tag" output/merged_epg.xml.gz \
  --repo "$GITHUB_REPOSITORY"

gh release edit "$release_tag" \
  --repo "$GITHUB_REPOSITORY" \
  --draft=false \
  --latest
