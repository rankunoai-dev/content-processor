"""Default wait for held tasks covers the render retry schedule (unattended runs)."""

from __future__ import annotations

from src.modules.content_processor.scheduler.runner import RunOptions
from src.modules.content_processor.workers.render_worker import RENDER_RETRY_DELAYS_S


def test_default_wait_covers_the_whole_render_retry_schedule() -> None:
    assert RunOptions().max_wait_s >= sum(RENDER_RETRY_DELAYS_S)
    assert RunOptions().max_wait_s == 360.0
