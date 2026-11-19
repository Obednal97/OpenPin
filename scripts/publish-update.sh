#!/usr/bin/env bash
# Tell the update Worker about a release straight away: writes the Worker's
# last-good answer in KV from the GitHub release, so installs are offered it
# even while GitHub refuses the Worker's unauthenticated API calls (it falls
# back to KV then; on 2026-11-19 that kept everyone on 2.11.2 after 2.12.1).
#
#   scripts/publish-update.sh 2.12.1
#
# Run it after `gh release create`. Writes the canary channel always and the
# stable channel only when the release is GitHub's Latest (not a pre-release),
# matching what the Worker itself would serve. Needs gh, jq, curl, npx and the
# Cloudflare credentials in the environment (CLOUDFLARE_API_TOKEN + _ACCOUNT_ID).
set -euo pipefail

V="${1:?usage: $0 <version, e.g. 2.12.1>}"
V="${V#v}"
TAG="v$V"
REPO=Obednal97/OpenPin
cd "$(dirname "$0")/../infra/update-worker"

REL="$(gh api "repos/$REPO/releases/tags/$TAG")"
DMG="$(jq -r '[.assets[] | select(.name | endswith(".dmg"))][0].name // empty' <<<"$REL")"
[ -n "$DMG" ] || { echo "$TAG has no .dmg asset" >&2; exit 1; }
jq -e --arg n "$DMG.sha256" 'any(.assets[]; .name == $n)' <<<"$REL" >/dev/null \
  || { echo "$TAG has no $DMG.sha256 asset" >&2; exit 1; }
SHA="$(curl -fsSL "https://github.com/$REPO/releases/download/$TAG/$DMG.sha256" | awk '{print $1}')"
[[ "$SHA" =~ ^[0-9a-fA-F]{64}$ ]] || { echo "Bad checksum in $DMG.sha256" >&2; exit 1; }

# Same shape and key order as the Worker's fetchLatest(): no download_url.
DATA="$(jq -c --arg sha "$SHA" --arg dmg "$DMG" '{
  version: (.tag_name | ltrimstr("v")),
  tag: .tag_name,
  name: (.name // .tag_name),
  notes: ((.body // "")[:20000]),
  published_at: .published_at,
  asset: ([.assets[] | select(.name == $dmg)][0] | {name, size, sha256: $sha})
}' <<<"$REL")"

LATEST="$(gh api "repos/$REPO/releases/latest" --jq .tag_name)"
CHANNELS=(canary)
if [ "$LATEST" = "$TAG" ]; then CHANNELS+=(stable); else
  echo "$TAG isn't GitHub's Latest ($LATEST is), so only canary is updated"
fi

TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT
printf '%s' "$DATA" >"$TMP"
for ch in "${CHANNELS[@]}"; do
  npx --yes wrangler@4 kv key put --binding LAST_GOOD "latest|$ch" --path "$TMP" --remote >/dev/null
  got="$(npx --yes wrangler@4 kv key get --binding LAST_GOOD "latest|$ch" --remote 2>/dev/null | jq -r .version)"
  [ "$got" = "$V" ] || { echo "KV latest|$ch reads back $got, not $V" >&2; exit 1; }
  echo "Update server ($ch): $V"
done
