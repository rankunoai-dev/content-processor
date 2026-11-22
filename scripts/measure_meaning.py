"""Score an embedder on the cross-language meaning set (RFC 0004, slice 3).

    python scripts/measure_meaning.py --embedder hashing
    python scripts/measure_meaning.py --embedder cpu
    python scripts/measure_meaning.py --embedder ollama --model bge-m3

`ollama` needs a running model server with the model pulled (CP_MODEL_ENDPOINT, default
http://localhost:11434/v1). Nothing is installed or downloaded by this script.
"""

from __future__ import annotations

import argparse
import json
import sys

from src.core.config import get_settings
from src.modules.content_processor.knowledge.embedders import HashingEmbedder, open_embedder
from src.modules.content_processor.knowledge.meaning_eval import evaluate


def main(argv: list[str] | None = None) -> int:
    """Parse the command line, score the chosen embedder, and print the JSON result."""
    parser = argparse.ArgumentParser(description="Cross-language meaning measurement")
    parser.add_argument("--embedder", choices=["hashing", "cpu", "ollama"], required=True)
    parser.add_argument(
        "--model", default=None, help="model name for ollama (default CP_EMBED_MODEL)"
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.embedder == "hashing":
        embedder = HashingEmbedder()
    elif args.embedder == "cpu":
        embedder, _note = open_embedder("cpu", settings.cp_model_endpoint, "")
    else:
        model = args.model or settings.cp_embed_model
        embedder, note = open_embedder("ollama", settings.cp_model_endpoint, model)
        sys.stderr.write(note + "\n")
    result = evaluate(embedder)
    sys.stdout.write(json.dumps(result.model_dump(), ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
