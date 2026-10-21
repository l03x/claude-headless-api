#!/usr/bin/env python3
"""Regenerate docs/openapi.json from docs/openapi.yaml (the source of truth).

    python scripts/export_openapi.py          # write the JSON
    python scripts/export_openapi.py --check  # exit 1 if the JSON is stale (used by the tests/CI)
"""

import json
import sys
from pathlib import Path

import yaml

DOCS = Path(__file__).resolve().parent.parent / "docs"


def render() -> str:
    spec = yaml.safe_load((DOCS / "openapi.yaml").read_text())
    return json.dumps(spec, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    out = DOCS / "openapi.json"
    content = render()
    if "--check" in sys.argv:
        if not out.exists() or out.read_text() != content:
            print("docs/openapi.json is out of date: run python scripts/export_openapi.py", file=sys.stderr)
            return 1
        return 0
    out.write_text(content)
    print(f"wrote {out.relative_to(DOCS.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
