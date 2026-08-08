#!/usr/bin/env python3
"""
ONNX Model Downloader for the Hailo-8 Edge Filter Service.

Downloads the Time-Series-Transformer ONNX model from a configurable URL
and places it at the path expected by HAILO8_MODEL_PATH.

Configuration via environment variables (or .env file):
  MODEL_DOWNLOAD_URL        Download URL (required — no default)
  MODEL_DOWNLOAD_SHA256     Expected SHA-256 hex digest for integrity check (optional)
  HAILO8_MODEL_PATH         Target path (default: /models/hailo/timeseries_transformer.onnx)
  MODEL_DOWNLOAD_RETRIES    Number of retry attempts (default: 3)
  MODEL_DOWNLOAD_TIMEOUT    HTTP timeout in seconds (default: 120)
  MODEL_DOWNLOAD_FORCE      Set to "true" to overwrite existing model (default: false)

Usage:
  python scripts/download_onnx_model.py
  MODEL_DOWNLOAD_URL=https://... python scripts/download_onnx_model.py
"""

import argparse
import hashlib
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("model_downloader")


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader — no external dependency required."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_with_progress(url: str, dest: Path, timeout: int) -> None:
    """Download *url* → *dest* with a simple progress indicator."""

    def _reporthook(count: int, block_size: int, total_size: int) -> None:
        if total_size <= 0:
            sys.stdout.write(f"\r  Downloaded {count * block_size / 1024:.0f} KB")
        else:
            pct = min(count * block_size / total_size * 100, 100)
            mb = count * block_size / 1024 / 1024
            total_mb = total_size / 1024 / 1024
            sys.stdout.write(f"\r  {pct:5.1f}%  {mb:.1f}/{total_mb:.1f} MB")
        sys.stdout.flush()

    # urllib does not accept a single timeout kwarg in urlretrieve; use opener
    opener = urllib.request.build_opener()
    opener.addheaders = [("User-Agent", "TradingBot/1.0 model-downloader")]

    import socket
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        urllib.request.urlretrieve(url, filename=str(dest), reporthook=_reporthook)
    finally:
        socket.setdefaulttimeout(old_timeout)
    sys.stdout.write("\n")


def download_model(
    url: str,
    target_path: Path,
    expected_sha256: str | None = None,
    retries: int = 3,
    timeout: int = 120,
    force: bool = False,
) -> Path:
    """
    Download the ONNX model to *target_path*.

    Args:
        url:            Source URL (http/https or file://).
        target_path:    Destination path for the model file.
        expected_sha256: If provided, verify integrity after download.
        retries:        Number of retry attempts on failure.
        timeout:        HTTP socket timeout in seconds.
        force:          Overwrite if file already exists.

    Returns:
        Resolved Path to the downloaded model.

    Raises:
        SystemExit on unrecoverable error.
    """
    target_path = Path(target_path).resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if target_path.exists() and not force:
        logger.info("Model already exists at %s", target_path)
        if expected_sha256:
            actual = _sha256_of_file(target_path)
            if actual.lower() == expected_sha256.lower():
                logger.info("✅ SHA-256 verified (existing file matches).")
                return target_path
            else:
                logger.warning(
                    "⚠️  SHA-256 mismatch on existing file — re-downloading.\n"
                    "  expected: %s\n  actual:   %s", expected_sha256, actual
                )
        else:
            logger.info("Skipping download (use --force to overwrite).")
            return target_path

    tmp_path = target_path.with_suffix(".tmp")

    for attempt in range(1, retries + 1):
        logger.info("Downloading model (attempt %d/%d)…", attempt, retries)
        logger.info("  From: %s", url)
        logger.info("  To:   %s", target_path)
        try:
            _download_with_progress(url, tmp_path, timeout)
            break
        except (OSError, urllib.error.URLError, ValueError) as exc:
            logger.warning("Download attempt %d failed: %s", attempt, exc)
            if tmp_path.exists():
                tmp_path.unlink()
            if attempt < retries:
                wait = 5 * attempt
                logger.info("Retrying in %d s…", wait)
                time.sleep(wait)
            else:
                logger.error("All %d download attempts failed. Exiting.", retries)
                sys.exit(1)

    # Integrity check
    if expected_sha256:
        actual = _sha256_of_file(tmp_path)
        if actual.lower() != expected_sha256.lower():
            logger.error(
                "❌ SHA-256 integrity check FAILED:\n"
                "  expected: %s\n  actual:   %s\n"
                "  The downloaded file may be corrupted or tampered with. Aborting.",
                expected_sha256, actual,
            )
            tmp_path.unlink(missing_ok=True)
            sys.exit(1)
        logger.info("✅ SHA-256 verified.")
    else:
        logger.warning(
            "No SHA-256 provided — skipping integrity check. "
            "Set MODEL_DOWNLOAD_SHA256 for tamper protection."
        )

    tmp_path.rename(target_path)
    size_mb = target_path.stat().st_size / 1024 / 1024
    logger.info("✅ Model saved: %s (%.1f MB)", target_path, size_mb)
    return target_path


def main() -> None:
    _load_dotenv()

    parser = argparse.ArgumentParser(description="Download ONNX model for Hailo-8 edge filter")
    parser.add_argument(
        "--url",
        default=os.getenv("MODEL_DOWNLOAD_URL", ""),
        help="Download URL (env: MODEL_DOWNLOAD_URL)",
    )
    parser.add_argument(
        "--output",
        default=os.getenv("HAILO8_MODEL_PATH", "/models/hailo/timeseries_transformer.onnx"),
        help="Target path (env: HAILO8_MODEL_PATH)",
    )
    parser.add_argument(
        "--sha256",
        default=os.getenv("MODEL_DOWNLOAD_SHA256", ""),
        help="Expected SHA-256 hex digest (env: MODEL_DOWNLOAD_SHA256)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=int(os.getenv("MODEL_DOWNLOAD_RETRIES", "3")),
        help="Retry attempts (env: MODEL_DOWNLOAD_RETRIES)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.getenv("MODEL_DOWNLOAD_TIMEOUT", "120")),
        help="HTTP timeout in seconds (env: MODEL_DOWNLOAD_TIMEOUT)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=os.getenv("MODEL_DOWNLOAD_FORCE", "false").lower() == "true",
        help="Overwrite existing model (env: MODEL_DOWNLOAD_FORCE=true)",
    )
    args = parser.parse_args()

    if not args.url:
        logger.error(
            "MODEL_DOWNLOAD_URL is not set.\n"
            "Set it via environment variable or pass --url <url>.\n"
            "Example:\n"
            "  MODEL_DOWNLOAD_URL=https://your-model-host/timeseries_transformer.onnx \\\n"
            "  python scripts/download_onnx_model.py"
        )
        sys.exit(1)

    download_model(
        url=args.url,
        target_path=Path(args.output),
        expected_sha256=args.sha256 or None,
        retries=args.retries,
        timeout=args.timeout,
        force=args.force,
    )


if __name__ == "__main__":
    main()
