"""
api/schemas.py — Pydantic models for API request and response bodies.

WHY SEPARATE SCHEMAS FROM INTERNAL MODELS?
-------------------------------------------
jobs/models.py defines JobRecord — the internal representation of a job,
including sensitive fields like repo_path (temp dir on disk) that we
never want to expose to API callers.

This file defines what we actually return to the user. Keeping them separate
means:
  • We control exactly what leaves the API boundary
  • Internal changes (adding a field to JobRecord) don't accidentally
    change the API contract
  • FastAPI uses these schemas to auto-generate the OpenAPI docs at /docs

FastAPI validates EVERY response against the response_model before sending it.
If a route accidentally puts a wrong type in the response, FastAPI catches it
here rather than sending malformed JSON.
"""
from typing import Optional

from pydantic import BaseModel

from app.jobs.models import JobStatus


class ReviewSubmitResponse(BaseModel):
    """
    Returned immediately after POST /review.
    The client uses job_id to poll for status.
    """
    job_id: str
    status: JobStatus      # will always be "pending" at submit time
    created_at: str        # ISO-8601 datetime string


class AgentProgress(BaseModel):
    """Which agents have finished vs. are still waiting."""
    completed_agents: list[str]
    pending_agents: list[str]


class ReviewStatusResponse(BaseModel):
    """
    Returned by GET /review/{job_id}.

    Fields that are set depend on the current status:
      pending  → just job_id + status
      running  → job_id + status + progress
      complete → job_id + status + report
      failed   → job_id + status + error (+ partial_report if any agent completed)

    Optional fields that aren't set are serialized as null in JSON.
    The client should check `status` first, then read the appropriate field.
    """
    job_id: str
    status: JobStatus
    progress: Optional[AgentProgress] = None
    report: Optional[dict] = None
    error: Optional[str] = None
    partial_report: Optional[dict] = None


class HealthResponse(BaseModel):
    status: str     # "ok"
    version: str
