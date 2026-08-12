"""
api/routes.py — The three HTTP endpoints that form the public API.

THE THREE ENDPOINTS:
--------------------
  POST /review         Accept ZIP, create job, return job_id (202)
  GET  /review/{id}    Poll status or retrieve completed report
  GET  /health         Liveness check

FASTAPI CORE CONCEPTS EXPLAINED HERE:
--------------------------------------

1. APIRouter
   Like a Flask Blueprint — groups related routes so main.py can register
   them all at once with app.include_router(router). You can also add a
   prefix here (prefix="/api/v1") if you want versioned URLs.

2. async def
   FastAPI route handlers should be async if they do I/O (reading files,
   awaiting background tasks, etc.). This lets the server handle other
   requests while yours is waiting for I/O.

3. BackgroundTasks
   FastAPI's built-in mechanism for "fire and forget" work. When you call
   background_tasks.add_task(fn, arg1, arg2), FastAPI will:
     a) Finish sending the HTTP response to the client
     b) THEN call fn(arg1, arg2) in the background
   This is how we return a 202 immediately while the review runs.

4. UploadFile = File(...)
   Tells FastAPI to expect a file upload in the request body (multipart/form-data).
   The `...` means required — FastAPI returns 422 automatically if it's missing.
   UploadFile gives us the filename and an async read() method.

5. response_model=
   FastAPI serializes the return value through this Pydantic model before
   sending it. Extra fields are stripped, required fields are validated.
   Also powers the /docs OpenAPI schema.

6. status_code=202
   202 Accepted = "I received your request and will process it asynchronously."
   The correct HTTP status for async jobs (vs 200 which implies a done result).
"""
import logging

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile

from app.api.schemas import (
    AgentProgress,
    HealthResponse,
    ReviewStatusResponse,
    ReviewSubmitResponse,
)
from app.config import settings
from app.graph.build_graph import TRACKABLE_NODES, review_graph
from app.jobs.models import JobRecord, JobStatus
from app.jobs.store import job_store
from app.sandbox.extractor import validate_and_extract

logger = logging.getLogger(__name__)

# APIRouter is the object we register with the FastAPI app in main.py.
# All routes defined here are prefixed with whatever main.py sets.
router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# POST /review
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/review", response_model=ReviewSubmitResponse, status_code=202)
async def submit_review(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="ZIP archive of the repository to review"),
):
    """
    Submit a repository for review.

    Accepts a .zip file, validates it, extracts it, creates a review job,
    and immediately returns the job_id. The actual review runs in the background.

    Use GET /review/{job_id} to poll for status and retrieve the report.
    """

    # ── Validate file type ─────────────────────────────────────────────────────
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(
            status_code=400,
            detail="Only .zip files are accepted. Please upload a ZIP archive of your repository.",
        )

    logger.info("Received upload: %s", file.filename)

    # ── Read bytes ─────────────────────────────────────────────────────────────
    # We read the full file into memory here. For v1 (25 MB limit) this is fine.
    # For larger limits you'd stream directly to disk instead.
    zip_bytes = await file.read()

    # ── Validate + extract ─────────────────────────────────────────────────────
    # This raises HTTPException(400) for any invalid input.
    extraction = validate_and_extract(
        zip_bytes=zip_bytes,
        filename=file.filename,
        max_size_mb=settings.max_upload_size_mb,
        max_files=settings.max_file_count,
    )

    logger.info(
        "Extracted %s: %d files, languages=%s",
        extraction.repo_name,
        extraction.file_count,
        extraction.detected_languages,
    )

    # ── Create job record ──────────────────────────────────────────────────────
    job = JobRecord(
        repo_name=extraction.repo_name,
        repo_path=extraction.repo_dir,
        status=JobStatus.PENDING,
        # All five agent names start as pending
        pending_agents=["static_analysis", "security", "performance", "style", "summary"],
    )
    job_store.create(job)

    # ── Schedule the background review ────────────────────────────────────────
    # FastAPI will call run_review() AFTER the 202 response is sent.
    # We pass job.job_id and extraction as arguments.
    background_tasks.add_task(_run_review, job.job_id, extraction)

    logger.info("Job %s created for repo %s", job.job_id, extraction.repo_name)

    return ReviewSubmitResponse(
        job_id=job.job_id,
        status=job.status,
        created_at=job.created_at.isoformat(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET /review/{job_id}
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/review/{job_id}", response_model=ReviewStatusResponse)
async def get_review_status(job_id: str):
    """
    Poll the status of a review job.

    Returns different payloads depending on job status:
    - pending/running: progress field shows which agents are done
    - complete:        report field contains the full review report
    - failed:          error field explains the failure; partial_report if available
    """
    job = job_store.get(job_id)

    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    # Build the response shape based on current job status
    response = ReviewStatusResponse(
        job_id=job.job_id,
        status=job.status,
    )

    if job.status in (JobStatus.PENDING, JobStatus.RUNNING):
        response.progress = AgentProgress(
            completed_agents=list(job.completed_agents),
            pending_agents=list(job.pending_agents),
        )

    elif job.status == JobStatus.COMPLETE:
        response.report = job.report

    elif job.status == JobStatus.FAILED:
        response.error = job.error
        # Return partial results if at least one agent finished before the failure
        if job.agent_results:
            response.partial_report = job.agent_results

    return response


# ─────────────────────────────────────────────────────────────────────────────
# GET /health
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/health", response_model=HealthResponse)
async def health():
    """
    Liveness check. Returns 200 OK if the service is running.

    Used by load balancers, container orchestrators (Kubernetes readiness
    probe), and monitoring systems to verify the service is alive.
    """
    return HealthResponse(status="ok", version="1.0.0")


# ─────────────────────────────────────────────────────────────────────────────
# Background task: the actual review pipeline
# ─────────────────────────────────────────────────────────────────────────────
async def _run_review(job_id: str, extraction) -> None:
    """
    Runs the full LangGraph review pipeline for one job.

    This function is called by FastAPI's BackgroundTasks AFTER the 202
    response has already been sent to the client. It:
      1. Marks the job as RUNNING
      2. Invokes the LangGraph graph via astream() (async streaming)
      3. Updates the job record as each node completes
      4. Writes the final report when the graph finishes
      5. Cleans up the temp directory

    HOW astream() WORKS:
    --------------------
    review_graph.astream(initial_state) is an async generator.
    It yields one dict per node completion event:
      {"static_analysis": {"static_analysis_findings": [...], ...}}

    The key is the node name. The value is that node's return dict.
    We use these events to update completed_agents / pending_agents in real time
    so GET /review/{job_id} can show accurate progress mid-run.
    """
    logger.info("Starting review pipeline for job %s", job_id)
    job_store.update_status(job_id, JobStatus.RUNNING)

    # Build the initial state — this is what every node starts with
    initial_state = {
        "job_id": job_id,
        "repo_path": extraction.repo_dir,
        "repo_name": extraction.repo_name,
        "detected_languages": extraction.detected_languages,
        # Agent output fields start as None
        "static_analysis_findings": None,
        "static_analysis_error": None,
        "security_findings": None,
        "security_error": None,
        "performance_findings": None,
        "performance_error": None,
        "style_findings": None,
        "style_error": None,
        "final_report": None,
        "summary_error": None,
    }

    try:
        # Stream node completion events
        async for event in review_graph.astream(initial_state):
            for node_name, node_output in event.items():
                if node_name not in TRACKABLE_NODES:
                    continue  # skip internal LangGraph bookkeeping events

                logger.info("Node '%s' completed for job %s", node_name, job_id)

                # Update job progress in the store
                job = job_store.get(job_id)
                if job:
                    if node_name in job.pending_agents:
                        job.pending_agents.remove(node_name)
                    if node_name not in job.completed_agents:
                        job.completed_agents.append(node_name)
                    # Accumulate agent outputs
                    job.agent_results.update(node_output)

        # ── Graph finished — extract the final report ──────────────────────────
        job = job_store.get(job_id)
        final_report = (job.agent_results.get("final_report") if job else None)

        if final_report:
            job_store.update_status(job_id, JobStatus.COMPLETE, report=final_report)
            logger.info("Job %s completed successfully", job_id)
        else:
            # Summary node ran but produced no report — shouldn't happen normally
            job_store.update_status(
                job_id,
                JobStatus.FAILED,
                error="Review pipeline completed but produced no report",
            )
            logger.error("Job %s: no final report produced", job_id)

    except Exception as exc:
        logger.exception("Job %s failed with unexpected error: %s", job_id, exc)
        job_store.update_status(job_id, JobStatus.FAILED, error=str(exc))

    finally:
        # Always clean up the extracted repo from disk, even on failure
        logger.info("Cleaning up temp dir for job %s", job_id)
        extraction.cleanup()
