"""skewlens: column profiling for join and group-by risk."""

from __future__ import annotations

__version__ = "0.1.0"

from .report import Risk, assess, rank, verify, write_reports  # noqa: E402
from .scan import ScannerError, scan  # noqa: E402
from .synth import Truth, generate  # noqa: E402

__all__ = [
    "Risk",
    "ScannerError",
    "Truth",
    "__version__",
    "assess",
    "generate",
    "rank",
    "scan",
    "verify",
    "write_reports",
]
