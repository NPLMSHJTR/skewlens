"""Run the TypeScript scanner and parse its profile."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

SCANNER = Path(__file__).resolve().parents[2] / "scanner" / "scan.ts"


class ScannerError(RuntimeError):
    """Raised when the Node scanner cannot run or returns invalid output."""


def scanner_path() -> Path:
    if not SCANNER.is_file():
        raise ScannerError(f"scanner not found at {SCANNER}; run skewlens from a source checkout")
    return SCANNER


def scan(input_path: Path, workers: int = 4, timeout: int = 900) -> dict[str, Any]:
    """Profile ``input_path`` and return the scanner's JSON payload."""
    node = shutil.which("node")
    if node is None:
        raise ScannerError("node is not on PATH; Node 22.6 or newer is required")

    command = [
        node,
        "--experimental-strip-types",
        str(scanner_path()),
        "--input",
        str(input_path),
        "--workers",
        str(max(1, workers)),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, encoding="utf-8"
        )
    except subprocess.TimeoutExpired as error:
        raise ScannerError(f"scanner timed out after {timeout}s") from error

    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        message = detail[-1] if detail else f"exit code {completed.returncode}"
        raise ScannerError(f"scanner failed: {message}")

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ScannerError("scanner returned output that is not JSON") from error
    if not isinstance(payload, dict) or "columns" not in payload:
        raise ScannerError("scanner returned an unexpected payload")
    return payload
