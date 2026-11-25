"""Vercel entrypoint for the LeadScope FastAPI app."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mangum import Mangum  # noqa: E402

from backend.main import app  # noqa: E402

handler = Mangum(app, lifespan="off")
