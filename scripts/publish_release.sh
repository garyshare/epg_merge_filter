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

gh release list --repo "$GITHUB_REPOSITORY" --limit 1000 --json tagName --jq '.[].tagName' |
  while read -r tag; do
    [ -n "$tag" ] && [ "$tag" != "$release_tag" ] || continue
    gh release delete "$tag" --repo "$GITHUB_REPOSITORY" --yes
  done

gh api --paginate "repos/${GITHUB_REPOSITORY}/git/matching-refs/tags/" --jq '.[].ref' |
  while read -r ref; do
    [ -n "$ref" ] && [ "$ref" != "refs/tags/${release_tag}" ] || continue
    gh api -X DELETE "repos/${GITHUB_REPOSITORY}/git/${ref}"
  done
