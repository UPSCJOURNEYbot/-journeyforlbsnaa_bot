#!/usr/bin/env python3
"""Uvicorn entrypoint for the Test Series PDF microservice.

Configuration (environment, all optional -- safe defaults):
  PDF_SERVICE_HOST  bind address (default 127.0.0.1 -- localhost only)
  PDF_SERVICE_PORT  bind port    (default 8090)

Run:  python -m pdf_service.server        (from the repo root)
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
for noisy in ("uvicorn.error", "uvicorn.access", "fontTools.subset",
               "fontTools.ttLib", "fontTools.subset.timer"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger("pdf_service.server")


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))

    try:
        import uvicorn

        from pdf_service.app import app
    except ImportError as exc:
        logger.error("Missing dependency for the PDF service: %s", exc)
        logger.error("Install with: pip install -r requirements.txt")
        sys.exit(1)

    host = os.getenv("PDF_SERVICE_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(os.getenv("PDF_SERVICE_PORT", "8090") or 8090)
    except ValueError:
        logger.error("PDF_SERVICE_PORT must be a number.")
        sys.exit(1)

    for font in ("Hind-Regular.ttf", "Hind-Bold.ttf"):
        if not (repo_root / "pdf_service" / "fonts" / font).is_file():
            logger.error("Missing bundled font: pdf_service/fonts/%s", font)
            sys.exit(1)

    logger.info("Starting Journey PDF service on %s:%d ...", host, port)
    # access_log off: the bot polls /api/progress every ~1.2s per job.
    uvicorn.run(app, host=host, port=port, log_level="warning",
                access_log=False)


if __name__ == "__main__":
    main()
