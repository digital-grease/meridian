#!/usr/bin/env python3
"""Export the Pydantic Manifest model to JSON Schema.

Writes site/schemas/manifest.schema.json. Run after any change to schema.py,
then commit the output so external tooling has a stable reference.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "site" / "src"))

from schema import SCHEMA_VERSION, Manifest  # noqa: E402

OUT = REPO / "site" / "schemas" / "manifest.schema.json"


def main() -> int:
    schema = Manifest.model_json_schema()
    schema["$id"] = "https://meridianaudit.org/schemas/manifest.schema.json"
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = f"Meridian manifest (v{SCHEMA_VERSION})"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT.relative_to(REPO)} (schema_version={SCHEMA_VERSION})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
