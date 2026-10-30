"""numpy for the knowledge base, with its math library kept to one thread.

The vectors here are small (one query against a few thousand rows), so extra
threads gain nothing; OpenBLAS would otherwise start one thread per core in every
process, which competes with the browsers of a run (or of the parallel test
workers). An explicit `OPENBLAS_NUM_THREADS` set by the operator is respected.
"""

from __future__ import annotations

import os

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import numpy as np  # noqa: E402 - must follow the thread setting

__all__ = ["np"]
