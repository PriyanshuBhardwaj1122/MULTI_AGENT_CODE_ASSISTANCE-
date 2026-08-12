"""
app/main.py — FastAPI application entrypoint.

THIS FILE DOES THREE THINGS ONLY:
1. Creates the FastAPI app instance
2. Registers the router (all endpoints)
3. Exports `app` so Uvicorn knows what to run

WHY SO MINIMAL?
---------------
Separation of concerns. main.py is the "wiring layer" — it plugs components
together but contains no logic itself. As the app grows you'll add:
  - CORS middleware (to allow browser frontends to call the API)
  - Request ID middleware (for distributed tracing)
  - Auth middleware (when multi-user support is added)
  - Multiple routers (v1, v2, admin)

All of that lives here, without cluttering routes.py with cross-cutting concerns.

HOW TO RUN:
-----------
  uvicorn app.main:app --reload

  `app.main` = Python module path (app/main.py)
  `app`      = the variable name of the FastAPI instance inside that module
  `--reload` = auto-restart when you save a file (development only)

After starting, open http://localhost:8000/docs for the auto-generated
interactive API documentation (powered by your Pydantic schemas).
"""
import logging

from fastapi import FastAPI

from app.api.routes import router

# ── Logging ────────────────────────────────────────────────────────────────────
# Basic config: show timestamps, module name, log level, and message.
# In production you'd ship structured JSON logs to a log aggregator.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Multi-Agent Code Review Assistant",
    description=(
        "Automated code review across five dimensions: static analysis, security, "
        "performance, style, and a synthesized summary. Upload a repository ZIP "
        "and receive a structured review report."
    ),
    version="1.0.0",
    # FastAPI will redirect /docs → interactive Swagger UI
    # and /redoc → ReDoc documentation automatically.
)

# ── Register routes ────────────────────────────────────────────────────────────
# All routes in routes.py (/review, /review/{id}, /health) become available.
app.include_router(router)
