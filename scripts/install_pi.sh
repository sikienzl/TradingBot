#!/usr/bin/env bash
# install_pi.sh
#
# One-shot installer for the Crypto Trading Bot on a Raspberry Pi.
# Downloads the specified release from GitHub, installs system dependencies,
# creates a dedicated user, and registers systemd services.
#
# Usage (as root or via sudo):
#   sudo bash install_pi.sh [VERSION]
#
# Examples:
#   sudo bash install_pi.sh          # installs latest release
#   sudo bash install_pi.sh v0.1.1   # installs specific version
#
# After installation:
#   1. Edit /opt/trading_2/.env with your API keys and settings.
#   2. sudo systemctl start trading-bot
# ---------------------------------------------------------------------------
set -euo pipefail

GITHUB_REPO="sikienzl/TradingBot"
INSTALL_DIR="/opt/trading_2"
SERVICE_USER="trading"
LOG_DIR="${INSTALL_DIR}/logs"

die()  { echo "ERROR: $*" >&2; exit 1; }
info() { echo "INFO:  $*"; }
ok()   { echo "OK:    $*"; }

# ── Root check ────────────────────────────────────────────────────────────────
if [[ "$(id -u)" -ne 0 ]]; then
  die "This installer must be run as root or via sudo."
fi

# ── Version resolution ────────────────────────────────────────────────────────
VERSION="${1:-}"
if [[ -z "$VERSION" ]]; then
  info "Fetching latest release tag from GitHub..."
  VERSION="$(curl -fsSL "https://api.github.com/repos/${GITHUB_REPO}/releases/latest" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])")"
fi
VERSION="${VERSION}"  # keep as-is, e.g. v0.1.1
PACKAGE_NAME="trading-bot-pi-${VERSION}"
ARCHIVE_NAME="${PACKAGE_NAME}.tar.gz"
DOWNLOAD_URL="https://github.com/${GITHUB_REPO}/releases/download/${VERSION}/${ARCHIVE_NAME}"
SHA256_URL="${DOWNLOAD_URL}.sha256"

info "Installing Crypto Trading Bot ${VERSION} to ${INSTALL_DIR}"
info "Download: ${DOWNLOAD_URL}"

# ── System dependencies ───────────────────────────────────────────────────────
info "Installing system packages..."
apt-get update -q
apt-get install -y -q \
  python3 python3-venv python3-pip \
  python3-dev build-essential pkg-config \
  libgomp1 \
  curl ca-certificates
ok "System packages installed."

# ── Dedicated service user ────────────────────────────────────────────────────
if ! id "$SERVICE_USER" &>/dev/null; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  info "System user '${SERVICE_USER}' created."
fi

# ── Backup existing installation ──────────────────────────────────────────────
if [[ -d "$INSTALL_DIR" ]]; then
  BACKUP="/opt/trading_2_backup_$(date +%Y%m%d_%H%M%S)"
  info "Backing up existing installation to ${BACKUP} ..."
  # Preserve user data only; skip .venv to save space
  mkdir -p "$BACKUP"
  for preserve in .env trade_journal.csv results logs data; do
    if [[ -e "${INSTALL_DIR}/${preserve}" ]]; then
      cp -r "${INSTALL_DIR}/${preserve}" "${BACKUP}/"
    fi
  done
  ok "Backup saved to ${BACKUP}"
fi

# ── Download & verify archive ─────────────────────────────────────────────────
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

info "Downloading ${ARCHIVE_NAME}..."
curl -fSL --progress-bar "$DOWNLOAD_URL" -o "${TMP_DIR}/${ARCHIVE_NAME}"

# Verify checksum if available
if curl -fsSL "$SHA256_URL" -o "${TMP_DIR}/${ARCHIVE_NAME}.sha256" 2>/dev/null; then
  info "Verifying SHA-256 checksum..."
  (cd "$TMP_DIR" && sha256sum -c "${ARCHIVE_NAME}.sha256")
  ok "Checksum verified."
else
  echo "WARN: No checksum file available – proceeding without verification."
fi

# ── Extract & install ─────────────────────────────────────────────────────────
info "Extracting to ${INSTALL_DIR}..."
mkdir -p "$INSTALL_DIR"
tar -xzf "${TMP_DIR}/${ARCHIVE_NAME}" -C "$TMP_DIR"
# Copy files but don't overwrite existing .env or journal
rsync -a --ignore-existing \
  "${TMP_DIR}/${PACKAGE_NAME}/.env.example" "${INSTALL_DIR}/" 2>/dev/null || true
rsync -a \
  --exclude='.env' \
  --exclude='trade_journal.csv' \
  --exclude='results/' \
  --exclude='logs/' \
  "${TMP_DIR}/${PACKAGE_NAME}/" "${INSTALL_DIR}/"
ok "Files installed to ${INSTALL_DIR}"

# Restore preserved user data from backup (if any)
if [[ -n "${BACKUP:-}" && -d "${BACKUP}" ]]; then
  for preserve in .env trade_journal.csv results logs data; do
    if [[ -e "${BACKUP}/${preserve}" ]]; then
      cp -r "${BACKUP}/${preserve}" "${INSTALL_DIR}/"
      info "Restored ${preserve} from backup."
    fi
  done
fi

# Ensure first-run default .env exists
if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
  cp "${INSTALL_DIR}/.env.example" "${INSTALL_DIR}/.env"
  echo "WARN: Created default .env from .env.example."
  echo "WARN: Edit ${INSTALL_DIR}/.env before starting the bot!"
fi

mkdir -p "$LOG_DIR"

pip_install_with_retry() {
  local req_file="$1"
  local attempt
  for attempt in 1 2 3; do
    if "${VENV}/bin/pip" install -r "$req_file" --retries 10 --timeout 120; then
      return 0
    fi
    echo "WARN: pip install failed (attempt ${attempt}/3). Retrying in $((attempt * 5))s ..."
    sleep $((attempt * 5))
  done
  return 1
}

# ── Python virtual environment ────────────────────────────────────────────────
VENV="${INSTALL_DIR}/.venv"
info "Creating Python virtual environment..."
python3 -m venv "$VENV"
"${VENV}/bin/pip" install --upgrade pip --retries 10 --timeout 120

REQ_PI="${INSTALL_DIR}/requirements-pi.txt"
REQ_FULL="${INSTALL_DIR}/requirements.txt"
if [[ -f "$REQ_PI" ]]; then
  info "Installing Pi requirements (${REQ_PI})..."
  pip_install_with_retry "$REQ_PI" || die "Failed to install Pi requirements after multiple attempts."
else
  info "Installing full requirements (${REQ_FULL})..."
  pip_install_with_retry "$REQ_FULL" || die "Failed to install requirements after multiple attempts."
fi
ok "Python environment ready at ${VENV}"

# ── File ownership ────────────────────────────────────────────────────────────
chown -R "${SERVICE_USER}:${SERVICE_USER}" "$INSTALL_DIR"
ok "Ownership set to ${SERVICE_USER}."

# ── systemd services ──────────────────────────────────────────────────────────
SYSTEMD_DIR="/etc/systemd/system"
info "Installing systemd services..."

for svc_file in trading-bot.service scorecard.service scorecard.timer; do
  SRC="${INSTALL_DIR}/deploy/${svc_file}"
  DEST="${SYSTEMD_DIR}/${svc_file}"
  if [[ -f "$SRC" ]]; then
    cp "$SRC" "$DEST"
    ok "Installed ${svc_file}"
  else
    echo "WARN: ${SRC} not found – ${svc_file} not installed."
  fi
done

systemctl daemon-reload

# Enable & start scorecard timer (runs weekly, low risk)
if [[ -f "${SYSTEMD_DIR}/scorecard.timer" ]]; then
  systemctl enable --now scorecard.timer
  ok "Scorecard timer enabled (runs every Sunday 09:00)."
fi

# Enable trading-bot service but do NOT start automatically
# (user must configure .env first)
if [[ -f "${SYSTEMD_DIR}/trading-bot.service" ]]; then
  systemctl enable trading-bot.service
  info "Trading bot service registered (NOT started yet)."
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo
echo "══════════════════════════════════════════════════════"
echo " Crypto Trading Bot ${VERSION} installed successfully!"
echo "══════════════════════════════════════════════════════"
echo
echo " Installation directory : ${INSTALL_DIR}"
echo " Python environment     : ${VENV}"
echo " Log directory          : ${LOG_DIR}"
echo
echo " ⚠️  Next steps:"
echo "  1. Edit /opt/trading_2/.env – set API keys, DRY_RUN, etc."
echo "  2. sudo systemctl start trading-bot"
echo "  3. sudo journalctl -u trading-bot -f   # watch logs"
echo
echo " Useful commands:"
echo "  sudo systemctl status  trading-bot"
echo "  sudo systemctl restart trading-bot"
echo "  sudo systemctl stop    trading-bot"
echo "  sudo systemctl list-timers             # see scorecard schedule"
echo "══════════════════════════════════════════════════════"
