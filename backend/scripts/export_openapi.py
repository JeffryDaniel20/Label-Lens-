"""Writes the FastAPI app's OpenAPI schema to a file without needing a live
server - `frontend/package.json`'s `generate:api-types` script consumes it.

Usage: python scripts/export_openapi.py [output_path]
"""

from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("LABELLENS_SECRET_KEY", "openapi-export-placeholder-key-1234567890")
os.environ.setdefault("LABELLENS_DATABASE_URL", "sqlite://")

from app.main import create_app  # noqa: E402
from app.platform.config import get_settings  # noqa: E402


def main() -> None:
    output_path = sys.argv[1] if len(sys.argv) > 1 else "../frontend/openapi.json"
    app = create_app(get_settings())
    with open(output_path, "w") as f:
        json.dump(app.openapi(), f, indent=2)
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
