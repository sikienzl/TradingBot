#!/usr/bin/env python3
"""
ONNX Model Downloader for the Hailo-8 Edge Filter Service.

Downloads the Time-Series-Transformer ONNX model from a configurable URL
and places it at the path expected by HAILO8_MODEL_PATH.

Configuration via environment variables (or .env file):
  MODEL_DOWNLOAD_URL        Download URL (required when a download is needed)
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
import fcntl
import hashlib
import http.client
import logging
import os
import re
import shutil
import sys
import tempfile
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

BUNDLE_FILES = (
    "timeseries_transformer.onnx",
    "timeseries_transformer.onnx.data",
    "model_config.json",
)


def _bundle_id(directory: Path, *, exact: bool) -> str:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"Bundle is not a regular directory: {directory}")
    if exact and {p.name for p in directory.iterdir()} != set(BUNDLE_FILES):
        raise ValueError(f"Existing bundle contains missing or unexpected files: {directory}")
    manifest = []
    for name in BUNDLE_FILES:
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"Missing or invalid bundle file: {path}")
        manifest.append(f"{name} {_sha256_of_file(path)}\n")
    return hashlib.sha256("".join(manifest).encode("ascii")).hexdigest()


def install_bundle(source: Path, root: Path, bundle_id: str) -> Path:
    """Publish an immutable, content-addressed bundle under a cooperative PVC lock."""
    if not re.fullmatch(r"[0-9a-f]{64}", bundle_id):
        raise ValueError("Bundle ID must be a lowercase SHA-256 digest")
    source, root = Path(source), Path(root)
    if _bundle_id(source, exact=False) != bundle_id:
        raise ValueError("Image bundle does not match the requested bundle ID")

    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".install.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        destination = root / bundle_id
        if destination.exists() or destination.is_symlink():
            if _bundle_id(destination, exact=True) != bundle_id:
                raise ValueError(f"Existing bundle does not match its ID: {destination}")
            return destination

        staging = Path(tempfile.mkdtemp(prefix=".bundle-", dir=root))
        try:
            for name in BUNDLE_FILES:
                shutil.copyfile(source / name, staging / name)
                with (staging / name).open("rb") as artifact:
                    os.fsync(artifact.fileno())
            if _bundle_id(staging, exact=True) != bundle_id:
                raise ValueError("Copied bundle does not match the requested bundle ID")
            directory_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            os.rename(staging, destination)
            directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return destination


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
    request = urllib.request.Request(
        url, headers={"User-Agent": "TradingBot/1.0 model-downloader"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response, dest.open("wb") as out:
        length = response.headers.get("Content-Length")
        total_size = int(length) if length is not None else None
        downloaded = 0
        while chunk := response.read(64 * 1024):
            out.write(chunk)
            downloaded += len(chunk)
            if total_size:
                sys.stdout.write(f"\r  {min(downloaded / total_size * 100, 100):5.1f}%")
            else:
                sys.stdout.write(f"\r  Downloaded {downloaded / 1024:.0f} KB")
            sys.stdout.flush()
        if total_size is not None and downloaded != total_size:
            raise OSError("Download length does not match Content-Length")
        if not downloaded:
            raise OSError("Downloaded file is empty")
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
    if expected_sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        logger.error("MODEL_DOWNLOAD_SHA256 must contain exactly 64 hexadecimal characters.")
        sys.exit(1)
    if retries < 1 or timeout < 1:
        logger.error("Retries and timeout must be positive integers.")
        sys.exit(1)

    target_path = Path(target_path).resolve()

    if target_path.exists() and not force:
        if not target_path.is_file() or target_path.stat().st_size == 0:
            logger.error("Existing model is not a non-empty regular file: %s", target_path)
            sys.exit(1)
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
            logger.warning(
                "Skipping download without verifying the existing file "
                "(use --force to overwrite)."
            )
            return target_path

    if not url:
        logger.error("MODEL_DOWNLOAD_URL is required when a download is needed.")
        sys.exit(1)

    target_path.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, retries + 1):
        logger.info("Downloading model (attempt %d/%d)…", attempt, retries)
        logger.info("  To:   %s", target_path)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{target_path.name}.", suffix=".part", dir=target_path.parent)
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            _download_with_progress(url, tmp_path, timeout)
            if expected_sha256:
                actual = _sha256_of_file(tmp_path)
                if actual.lower() != expected_sha256.lower():
                    logger.error("SHA-256 mismatch; downloaded file was not installed.")
                    sys.exit(1)
                logger.info("✅ SHA-256 verified.")
            else:
                logger.warning(
                    "No SHA-256 provided — skipping integrity check. "
                    "A digest from a trusted source can detect mismatched content."
                )
            size_mb = tmp_path.stat().st_size / 1024 / 1024
            os.replace(tmp_path, target_path)
            logger.info("✅ Model saved: %s (%.1f MB)", target_path, size_mb)
            return target_path
        except (OSError, urllib.error.URLError, ValueError, http.client.HTTPException) as exc:
            # Exception messages may contain URL credentials or query parameters.
            logger.warning("Download attempt %d failed (%s).", attempt, type(exc).__name__)
            if attempt < retries:
                wait = 5 * attempt
                logger.info("Retrying in %d s…", wait)
                time.sleep(wait)
            else:
                logger.error("All %d download attempts failed. Exiting.", retries)
                sys.exit(1)
        finally:
            tmp_path.unlink(missing_ok=True)


def main() -> None:
    _load_dotenv()

    parser = argparse.ArgumentParser(description="Download ONNX model for Hailo-8 edge filter")
    parser.add_argument("--bundle-source", help="Install the three image artifacts as one bundle")
    parser.add_argument("--bundle-root", default="/models/bundles", help="PVC bundle directory")
    parser.add_argument("--bundle-id", help="Expected content-addressed bundle ID")
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

    if args.bundle_source:
        try:
            path = install_bundle(Path(args.bundle_source), Path(args.bundle_root), args.bundle_id or "")
        except (OSError, ValueError) as exc:
            logger.error("Bundle installation failed: %s", exc)
            sys.exit(1)
        logger.info("Verified bundle at %s", path)
        return
    if args.bundle_id:
        parser.error("--bundle-id requires --bundle-source")

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
