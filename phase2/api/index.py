"""Vercel @vercel/python entry point.

Re-exports the FastAPI `app` from main.py at the project root so Vercel's
serverless runtime can mount it. The vercel.json rewrites all incoming
paths to /api/index, where this module lives.

Local dev still uses `uvicorn main:app` directly — this file is only for
the deployed environment.
"""

from main import app  # noqa: F401  (re-export for Vercel)
