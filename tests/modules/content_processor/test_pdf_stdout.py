"""K18: PDF extraction must not write to stdout (the CLI prints JSON there)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = (
    "import pymupdf\n"
    "doc = pymupdf.open()\n"
    "doc.new_page().insert_text((72, 72), 'Fee schedule 2026')\n"
    "data = doc.tobytes()\n"
    "from src.modules.content_processor.adapters.pdf import extract_pdf\n"
    "extract_pdf(data, resource_id='r', run_id='run', evidence_id='e')\n"
)


def test_pdf_extraction_prints_nothing_to_stdout():
    done = subprocess.run(  # noqa: S603 - fixed interpreter and script
        [sys.executable, "-c", SCRIPT],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "", done.stdout
