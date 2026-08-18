"""
jobs/store.py — In-memory job store for v1.

WHAT IS THIS?
-------------
When a user submits a ZIP, we create a job record and need somewhere to keep it
so the GET /review/{job_id} endpoint can look it up later. This module is that
"somewhere" — a plain Python dict wrapped in a class with helper methods.

WHY A CLASS WRAPPER AND NOT JUST A DICT?
-----------------------------------------
Encapsulation. The rest of the codebase calls job_store.get(job_id) — it doesn't
need to know whether the underlying storage is a dict, Redis, or Postgres. In v2,
when we swap to Redis, we only change THIS file. Every other file that does
`from app.jobs.store import job_store` keeps working unchanged.

This pattern is called the Repository Pattern — a thin abstraction over your
persistence layer.

WHY NO ASYNC?
-------------
Dict reads/writes are in-memory and take nanoseconds — there's nothing to await.
When we move to Redis in v2 the methods will become async (redis-py has an async
client), but for now sync is correct and simpler.

THREADING NOTE:
---------------
FastAPI runs on an async event loop. Background tasks run on the same event loop.
In our v1 setup only one background task runs at a time per request, so we don't
need a threading lock. In a multi-worker production setup you'd need Redis (which
handles concurrency for you) instead of this in-memory store.
"""
from datetime import datetime, timezone
from typing import Any, Optional

from app.jobs.models import JobRecord, JobStatus


class InMemoryJobStore:
    def __init__(self):
        # The actual storage: job_id (str) → JobRecord
        self._jobs: dict[str, JobRecord] = {}

    def create(self, job: JobRecord) -> JobRecord:
        """Persist a new job record. Returns the same record."""
        self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> Optional[JobRecord]:
        """Look up a job by id. Returns None if not found."""
        return self._jobs.get(job_id)

    def update_status(
        self,
        job_id: str,
        status: JobStatus,
        **kwargs,
    ) -> Optional[JobRecord]:
        """
        Update a job's status plus any additional fields passed as kwargs.

        Example:
            job_store.update_status(job_id, JobStatus.FAILED, error="timed out")

        The **kwargs lets us pass optional extra fields (error, report, etc.)
        without needing a separate method for each combination.
        """
        job = self._jobs.get(job_id)
        if not job:
            return None

        job.status = status
        job.updated_at = datetime.utcnow()

        for key, value in kwargs.items():
            if hasattr(job, key):
                setattr(job, key, value)

        return job

    def all_jobs(self) -> list[JobRecord]:
        """Return all jobs — useful for debugging / admin tooling."""
        return list(self._jobs.values())

    def push_event(self, job_id: str, event_type: str, data: dict[str, Any]) -> None:
        """
        Append a streaming event to the job's event queue (M6).

        Called from _run_review() whenever a node completes or the job
        transitions to a terminal state. The SSE endpoint reads job.events
        from an offset to stream only new events to the client.

        event_type values:
            "node_complete"  — one analysis agent finished
            "job_complete"   — the whole review finished (status=complete)
            "job_failed"     — the review failed (status=failed)

        data: arbitrary dict, serialized as the SSE `data:` payload.
        """
        job = self._jobs.get(job_id)
        if not job:
            return
        job.events.append({
            "type": event_type,
            "data": data,
            "ts": datetime.now(timezone.utc).isoformat(),
        })


# ─── Module-level singleton ────────────────────────────────────────────────────
# One shared store for the lifetime of the process.
# All modules import this same instance:
#   from app.jobs.store import job_store
job_store = InMemoryJobStore()
