"""Self-hosted Test Series PDF microservice for Journey for लबासना.

Standalone FastAPI service (no Telegram code, no polling client) that fulfils
the EXISTING bot contract expected by `quizbot/creator_bot/handlers/reports.py`:

  POST {PDF_API_BASE}/api/generate   -> {"progress_url": ..., "download_url": ...}
  GET  {PDF_API_BASE}/api/progress/<job>
  GET  {PDF_API_BASE}/api/download/<job>

Rendering uses fpdf2 + HarfBuzz (uharfbuzz) with vendored OFL Hind fonts, so
Devanagari shaping is correct with zero system-library dependencies.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
