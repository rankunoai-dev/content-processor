"""Call one engine tool on the John Hancock project, as Claude Desktop would (read-only).

Usage (from the repo root): python docs/eval/analyst-100-2026-11-26/tool.py <tool> '<json input>'
       python docs/eval/analyst-100-2026-11-26/tool.py --list
Prints the tool's JSON answer (shortened past 12,000 characters).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.core.config import get_settings  # noqa: E402
from src.modules.analyst.playground import Playground  # noqa: E402
from src.modules.analyst.tools import TOOLS, run_tool  # noqa: E402
from src.modules.content_processor.projects.store import ProjectStore  # noqa: E402
from src.modules.content_processor.webapi.__main__ import knowledge_access  # noqa: E402

PID = "john-hancock-test-5-31372c"
LIMIT = 12_000


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]  # Windows console
    if len(sys.argv) >= 2 and sys.argv[1] == "--list":
        for name, spec in TOOLS.items():
            built = "" if spec.built else " (NOT BUILT)"
            print(f"## {name}{built}\n{spec.description}\ninput: {json.dumps(spec.schema())}\n")
        return
    name = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    play = Playground(ProjectStore(get_settings().cp_app_data_root), knowledge_access(), [PID])
    text, err = run_tool(play, name, args)
    out = ("ERROR " if err else "") + text
    print(out[:LIMIT] + (f"\n... [{len(out) - LIMIT} more characters]" if len(out) > LIMIT else ""))


main()
