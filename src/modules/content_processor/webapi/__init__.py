"""Read-only analyst API (Lane L8): serves run results and the web app in `webui/`.

    python -m src.modules.content_processor.webapi --exports <dir> [--port 8780]
        [--static webui/dist]

See docs/UI_PLAN.md for the screens and endpoints.
"""

from __future__ import annotations

from src.modules.content_processor.webapi.app import create_app

__all__ = ["create_app"]
