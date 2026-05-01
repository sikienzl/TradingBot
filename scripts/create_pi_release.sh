#!/usr/bin/env bash
# create_pi_release.sh
#
# Builds a Raspberry Pi runtime package, creates a git tag, and pushes a
# GitHub release (using the GitHub Releases API via curl).
#
# Usage:
#   bash scripts/create_pi_release.sh [VERSION]
#
# Examples:
#   bash scripts/create_pi_release.sh            # auto-increment patch
#   bash scripts/create_pi_release.sh v1.2.3     # explicit version
#
# Required env vars for GitHub release upload (optional – skip to only tag):
#   GITHUB_TOKEN   personal access token with repo scope
#   GITHUB_REPO    owner/repo  (defaults to remote origin)
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# ── Helpers ──────────────────────────────────────────────────────────────────
die() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "INFO: $*"; }

# ── Version resolution ───────────────────────────────────────────────────────
if [[ -n "${1:-}" ]]; then
  VERSION="${1#v}"   # strip leading v if passed
else
  # Auto-increment: find latest vX.Y.Z tag and bump patch
  LAST_TAG="$(git tag --list 'v*' --sort=-version:refname | head -1 2>/dev/null || true)"
  if [[ -z "$LAST_TAG" ]]; then
    VERSION="0.1.0"
  else
    IFS='.' read -r -a PARTS <<< "${LAST_TAG#v}"
    MAJOR="${PARTS[0]:-0}"
    MINOR="${PARTS[1]:-1}"
    PATCH="${PARTS[2]:-0}"
    VERSION="${MAJOR}.${MINOR}.$((PATCH + 1))"
  fi
fi
TAG="v${VERSION}"
PACKAGE_NAME="trading-bot-pi-${TAG}"
ARCHIVE_NAME="${PACKAGE_NAME}.tar.gz"
BUILD_DIR="$(mktemp -d)"
STAGE="${BUILD_DIR}/${PACKAGE_NAME}"
trap 'rm -rf "$BUILD_DIR"' EXIT

info "Building release ${TAG} ..."

# ── Stage files ──────────────────────────────────────────────────────────────
mkdir -p "$STAGE"

INCLUDE_FILES=(
  # Python source
  trading_bot.py
  get_data.py
  data_preperation.py
  predict.py
  predict_catboost.py
  research_signal.py
  go_no_go_scorecard.py
  analyze_trade_journal.py
  get_account_balance.py
  # Config / documentation
  requirements.txt
  requirements-pi.txt
  README.md
  SERVER_README.md
  .env.example
  .env.live.example
  # Deploy helpers
  deploy
  scripts/run_weekly_scorecard.sh
  scripts/run_node_exporter_textfile_example.sh
  scripts/start_sim_bot.sh
  scripts/stop_sim_bot.sh
  scripts/install_pi.sh
  scripts/install_monitoring_pi.sh
  scripts/check_autoresearch_setup.sh
  scripts/update_autoresearch_signal.py
)

for item in "${INCLUDE_FILES[@]}"; do
  if [[ -f "$ROOT_DIR/$item" ]]; then
    dest_dir="$STAGE/$(dirname "$item")"
    mkdir -p "$dest_dir"
    cp "$ROOT_DIR/$item" "$dest_dir/"
  elif [[ -d "$ROOT_DIR/$item" ]]; then
    cp -r "$ROOT_DIR/$item" "$STAGE/"
  else
    echo "WARN: $item not found, skipping."
  fi
done

# Model artefacts (optional – only if present)
if [[ -d "$ROOT_DIR/model/catboost_trading_model" ]]; then
  mkdir -p "$STAGE/model"
  cp -r "$ROOT_DIR/model/catboost_trading_model" "$STAGE/model/"
  info "CatBoost model included."
else
  echo "WARN: model/catboost_trading_model not found, skipping."
fi

# Ensure results scaffold exists so scripts don't fail on first run
mkdir -p "$STAGE/results/scorecards"
mkdir -p "$STAGE/logs"
mkdir -p "$STAGE/data"

# ── Version file inside package ───────────────────────────────────────────────
cat > "$STAGE/version.txt" <<EOF
version=${TAG}
build_date=$(date -Iseconds)
build_host=$(hostname)
git_commit=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
EOF

# ── Artifact sanity checks (release blocking) ───────────────────────────────
REQUIRED_STAGE_FILES=(
  "${PACKAGE_NAME}/trading_bot.py"
  "${PACKAGE_NAME}/predict_catboost.py"
  "${PACKAGE_NAME}/research_signal.py"
  "${PACKAGE_NAME}/scripts/install_pi.sh"
  "${PACKAGE_NAME}/scripts/run_weekly_scorecard.sh"
  "${PACKAGE_NAME}/version.txt"
)

for required in "${REQUIRED_STAGE_FILES[@]}"; do
  if [[ ! -e "$BUILD_DIR/$required" ]]; then
    die "Missing required staged file: $required"
  fi
done

# ── Archive ───────────────────────────────────────────────────────────────────
info "Creating archive ${ARCHIVE_NAME} ..."
tar -czf "${ROOT_DIR}/${ARCHIVE_NAME}" -C "$BUILD_DIR" "$PACKAGE_NAME"

# Verify required files are present in archive before tagging/release upload.
for required in "${REQUIRED_STAGE_FILES[@]}"; do
  if ! tar -tzf "${ROOT_DIR}/${ARCHIVE_NAME}" "$required" >/dev/null 2>&1; then
    die "Missing required file in archive: $required"
  fi
done

SHA256="$(sha256sum "${ROOT_DIR}/${ARCHIVE_NAME}" | awk '{print $1}')"
info "Archive: ${ROOT_DIR}/${ARCHIVE_NAME}"
info "SHA-256: ${SHA256}"

# Write checksum file alongside archive
echo "$SHA256  ${ARCHIVE_NAME}" > "${ROOT_DIR}/${ARCHIVE_NAME}.sha256"

# ── Git tag ───────────────────────────────────────────────────────────────────
if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "WARN: Tag $TAG already exists locally, skipping tag creation."
else
  git tag -a "$TAG" -m "Raspberry Pi release ${TAG}"
  info "Tag ${TAG} created."
fi

git push origin "$TAG"
info "Tag ${TAG} pushed to origin."

# ── GitHub Release API ────────────────────────────────────────────────────────
GITHUB_TOKEN="${GITHUB_TOKEN:-}"
if [[ -z "$GITHUB_TOKEN" ]]; then
  info "GITHUB_TOKEN not set – skipping GitHub Release upload."
  info "To upload manually:"
  info "  GITHUB_TOKEN=<token> bash scripts/create_pi_release.sh ${TAG}"
  info "Archive is ready at: ${ROOT_DIR}/${ARCHIVE_NAME}"
  exit 0
fi

# Detect repo from remote if not explicitly set
GITHUB_REPO="${GITHUB_REPO:-}"
if [[ -z "$GITHUB_REPO" ]]; then
  REMOTE_URL="$(git remote get-url origin)"
  # Handle ssh: git@github.com:owner/repo.git and https: https://github.com/owner/repo.git
  GITHUB_REPO="$(echo "$REMOTE_URL" | sed -E 's|.*github\.com[:/]||;s|\.git$||')"
fi
info "GitHub repo: ${GITHUB_REPO}"

API_BASE="https://api.github.com"
AUTH_HEADER="Authorization: Bearer ${GITHUB_TOKEN}"

# Create release
info "Creating GitHub Release ${TAG} ..."
RELEASE_PAYLOAD="$(printf '{"tag_name":"%s","name":"Raspberry Pi Release %s","body":"## Raspberry Pi Release %s\n\n**SHA-256:** `%s`\n\n### Installation\n```bash\ncurl -fsSL https://github.com/%s/releases/download/%s/install_pi.sh | bash -s -- %s\n```\nSee SERVER_README.md for full instructions.","draft":false,"prerelease":false}' \
  "$TAG" "$TAG" "$TAG" "$SHA256" "$GITHUB_REPO" "$TAG" "$TAG")"

RELEASE_RESPONSE="$(curl -fsSL \
  -X POST \
  -H "$AUTH_HEADER" \
  -H "Content-Type: application/json" \
  -d "$RELEASE_PAYLOAD" \
  "${API_BASE}/repos/${GITHUB_REPO}/releases")"

UPLOAD_URL="$(echo "$RELEASE_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['upload_url'])" | sed 's/{.*}//')"

if [[ -z "$UPLOAD_URL" ]]; then
  die "Failed to create GitHub Release. Response: $RELEASE_RESPONSE"
fi

# Upload archive
info "Uploading ${ARCHIVE_NAME} ..."
curl -fsSL \
  -X POST \
  -H "$AUTH_HEADER" \
  -H "Content-Type: application/gzip" \
  --data-binary @"${ROOT_DIR}/${ARCHIVE_NAME}" \
  "${UPLOAD_URL}?name=${ARCHIVE_NAME}" > /dev/null

# Upload checksum
info "Uploading checksum ..."
curl -fsSL \
  -X POST \
  -H "$AUTH_HEADER" \
  -H "Content-Type: text/plain" \
  --data-binary @"${ROOT_DIR}/${ARCHIVE_NAME}.sha256" \
  "${UPLOAD_URL}?name=${ARCHIVE_NAME}.sha256" > /dev/null

# Upload standalone installer
info "Uploading install_pi.sh ..."
curl -fsSL \
  -X POST \
  -H "$AUTH_HEADER" \
  -H "Content-Type: text/x-shellscript" \
  --data-binary @"${ROOT_DIR}/scripts/install_pi.sh" \
  "${UPLOAD_URL}?name=install_pi.sh" > /dev/null

info "───────────────────────────────────────────────────"
info "Release ${TAG} published!"
info "URL: https://github.com/${GITHUB_REPO}/releases/tag/${TAG}"
info "Archive: ${ROOT_DIR}/${ARCHIVE_NAME}"
info "SHA-256: ${SHA256}"
info "───────────────────────────────────────────────────"
