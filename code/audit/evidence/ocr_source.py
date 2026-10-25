"""Optional local OCR for screenshot evidence.

Eleven of the supplied evidence files are application screenshots (a customer
console, an object browser, a retention screen).  They are business artefacts,
not deletion verdicts: the facts worth extracting are the resource ids, the
subject reference and the record type printed on screen.

Reading them needs OCR, which is **not** part of the default run:

* the default pipeline never calls out to an external binary, so a clean run is
  byte-for-byte reproducible on a machine without OCR tooling;
* nothing is sent anywhere - this module only shells out to a locally installed
  ``tesseract`` binary that the caller explicitly enabled (``--ocr`` or
  ``HR_AUDIT_OCR=1``).

When OCR is unavailable the reader returns an empty string and the case keeps
the deterministic behaviour described in the evaluation report.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

#: Hard ceiling on a single OCR call so a pathological image cannot hang a run.
_TIMEOUT_SECONDS = 120


def enabled_by_environment() -> bool:
    """True when ``HR_AUDIT_OCR`` asks for OCR."""

    return (os.environ.get("HR_AUDIT_OCR") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def available() -> bool:
    """True when a local ``tesseract`` binary can be executed."""

    return shutil.which("tesseract") is not None


def extract_text(path: Path) -> str:
    """Return the text a local OCR engine reads from ``path``.

    Returns an empty string when OCR is unavailable, times out, or fails; the
    caller then behaves exactly as it does without OCR.
    """

    binary: Optional[str] = shutil.which("tesseract")
    if binary is None:
        return ""
    try:
        completed = subprocess.run(
            [binary, str(path), "stdout"],
            capture_output=True,
            check=False,
            timeout=_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.decode("utf-8", errors="replace")
