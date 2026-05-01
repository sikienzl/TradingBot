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
NOTES_NAME="release-notes-${TAG}.txt"
NOTES_FILE="${ROOT_DIR}/${NOTES_NAME}"
RELEASE_BODY_FILE="${ROOT_DIR}/release-body-${TAG}.md"
CUSTOM_NOTES_SOURCE="${ROOT_DIR}/release_notes/${TAG}.txt"
BUILD_DIR="$(mktemp -d)"
STAGE="${BUILD_DIR}/${PACKAGE_NAME}"
trap 'rm -rf "$BUILD_DIR"; rm -f "$RELEASE_BODY_FILE"' EXIT

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
  # Monitoring provisioning
  deploy/grafana-datasource.yml
  deploy/grafana-dashboard-provisioning.yml
  deploy/grafana-dashboard.json
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

# ── Release notes (auto-generated) ───────────────────────────────────────────
PREV_TAG="$(git tag --list 'v*' --sort=-version:refname | grep -vx "$TAG" | head -1 || true)"
REPO_FOR_NOTES="$(git remote get-url origin | sed -E 's|.*github\.com[:/]||;s|\.git$||')"
if [[ -n "$PREV_TAG" ]]; then
  CHANGE_RANGE="${PREV_TAG}..HEAD"
  COMMITS="$(git --no-pager log --pretty='- %h %s' ${CHANGE_RANGE} | head -20 || true)"
  CHANGED_FILES="$(git --no-pager diff --name-only ${CHANGE_RANGE} | sed 's/^/- /' | head -40 || true)"
else
  CHANGE_RANGE="HEAD"
  COMMITS="$(git --no-pager log -1 --pretty='- %h %s' HEAD || true)"
  CHANGED_FILES="$(git --no-pager show --pretty='' --name-only HEAD | sed 's/^/- /' | sed '/^- $/d' | head -40 || true)"
fi
if [[ -z "$COMMITS" ]]; then
  COMMITS="- No commit summary available."
fi
if [[ -z "$CHANGED_FILES" ]]; then
  CHANGED_FILES="- No file list available."
fi

HIGHLIGHTS="$(printf '%s\n' "$COMMITS" | sed '/^$/d' | head -6)"
if [[ -z "$HIGHLIGHTS" ]]; then
  HIGHLIGHTS="- No highlights available."
fi

OPERATIONAL_IMPACT="- Review the highlights above before rollout; they summarize the release delta.
- Validate the changed components below in the target environment after deployment."
if printf '%s\n' "$CHANGED_FILES" | grep -Eq 'grafana|prometheus|scorecard|run_weekly_scorecard|go_no_go_scorecard'; then
  OPERATIONAL_IMPACT="${OPERATIONAL_IMPACT}
- Monitoring and scorecard outputs changed; confirm dashboards and exported metrics refresh as expected."
fi
if printf '%s\n' "$CHANGED_FILES" | grep -Eq 'trading_bot|predict|research_signal|model/'; then
  OPERATIONAL_IMPACT="${OPERATIONAL_IMPACT}
- Runtime trading behavior or model packaging changed; validate the deployed bot before enabling live execution."
fi

VERIFICATION_CHECKLIST="- Release assets download successfully and checksum verification passes."
if printf '%s\n' "$CHANGED_FILES" | grep -Eq 'scorecard|run_weekly_scorecard|go_no_go_scorecard'; then
  VERIFICATION_CHECKLIST="${VERIFICATION_CHECKLIST}
- Scorecard services/timers run successfully and refresh status outputs after deployment."
fi
if printf '%s\n' "$CHANGED_FILES" | grep -Eq 'grafana|prometheus|scorecard-status|node-exporter'; then
  VERIFICATION_CHECKLIST="${VERIFICATION_CHECKLIST}
- Prometheus exports update and Grafana panels show the expected current values."
fi
if printf '%s\n' "$CHANGED_FILES" | grep -Eq 'trading_bot|predict|research_signal|model/'; then
  VERIFICATION_CHECKLIST="${VERIFICATION_CHECKLIST}
- Trading bot startup and logs look healthy in the intended runtime profile."
fi

NOTES_SECTION="- Review the changed components before rolling out to production."
if [[ "$TAG" == *-rc* ]]; then
  NOTES_SECTION="${NOTES_SECTION}
- This is a release candidate. Keep production safeguards enabled."
fi
NOTES_SECTION="${NOTES_SECTION}
- Revoke and rotate any credentials that may have been exposed in logs, terminals, or chat history."

cat > "$RELEASE_BODY_FILE" <<EOF
## Crypto Trading Bot ${TAG}

### Highlights

${HIGHLIGHTS}

### Notes Asset

Detailed notes are attached as `${NOTES_NAME}`.

### Raspberry Pi Quick Install

Download and run the installer on your Pi (as root):

```bash
curl -fsSL https://github.com/${REPO_FOR_NOTES}/releases/download/${TAG}/install_pi.sh | sudo bash -s -- ${TAG}
```

Or manually:

```bash
# 1. Download the package
curl -LO https://github.com/${REPO_FOR_NOTES}/releases/download/${TAG}/${ARCHIVE_NAME}
# 2. Verify checksum
curl -LO https://github.com/${REPO_FOR_NOTES}/releases/download/${TAG}/${ARCHIVE_NAME}.sha256
sha256sum -c ${ARCHIVE_NAME}.sha256
# 3. Extract
tar -xzf ${ARCHIVE_NAME}
# 4. Run installer
sudo bash scripts/install_pi.sh ${TAG}
```

### After installation

1. Edit `/opt/trading_2/.env` - set your API keys.
2. `sudo systemctl start trading-bot`
3. `sudo journalctl -u trading-bot -f`

### What's included

- Runtime Python scripts
- Pi-optimised `requirements-pi.txt`
- Systemd service and timer units (`deploy/`)
- One-shot installer (`scripts/install_pi.sh`)

EOF

if [[ -f "$CUSTOM_NOTES_SOURCE" ]]; then
  cp "$CUSTOM_NOTES_SOURCE" "$NOTES_FILE"
else
cat > "$NOTES_FILE" <<EOF
Trading Bot Pi Release Notes - ${TAG}

Date: $(date -I)

Highlights

${HIGHLIGHTS}

Operational Impact

${OPERATIONAL_IMPACT}

Changed Components

${CHANGED_FILES}

Verification Checklist

${VERIFICATION_CHECKLIST}

Notes

${NOTES_SECTION}
EOF
fi

info "Release notes generated: ${NOTES_FILE}"

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
  info "Notes are ready at: ${NOTES_FILE}"
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
RELEASE_BODY_JSON="$(python3 - <<'PY' "$RELEASE_BODY_FILE"
import json
import pathlib
import sys
print(json.dumps(pathlib.Path(sys.argv[1]).read_text()))
PY
)"
RELEASE_PAYLOAD="$(printf '{"tag_name":"%s","name":"Raspberry Pi Release %s","body":%s,"draft":false,"prerelease":false}' \
  "$TAG" "$TAG" "$RELEASE_BODY_JSON")"

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

# Upload release notes
info "Uploading ${NOTES_NAME} ..."
curl -fsSL \
  -X POST \
  -H "$AUTH_HEADER" \
  -H "Content-Type: text/plain" \
  --data-binary @"${NOTES_FILE}" \
  "${UPLOAD_URL}?name=${NOTES_NAME}" > /dev/null

info "───────────────────────────────────────────────────"
info "Release ${TAG} published!"
info "URL: https://github.com/${GITHUB_REPO}/releases/tag/${TAG}"
info "Archive: ${ROOT_DIR}/${ARCHIVE_NAME}"
info "SHA-256: ${SHA256}"
info "Notes: ${NOTES_FILE}"
info "───────────────────────────────────────────────────"
