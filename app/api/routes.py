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
"""
api/routes.py — The HTTP endpoints that form the public API.

THE ENDPOINTS:
--------------
  POST /review                Accept ZIP, create job, return job_id (202)
  GET  /review/{id}           Poll status or retrieve completed report
  GET  /review/{id}/stream    SSE stream — push events as agents complete (M6)
  GET  /health                Liveness check

M6 — SERVER-SENT EVENTS:
--------------------------
GET /review/{job_id}/stream returns a text/event-stream response.
The client opens a persistent HTTP connection; the server pushes one SSE
frame per event (node_complete, job_complete, job_failed, heartbeat).

SSE format (RFC 8895):
    data: {"type":"node_complete","data":{"node":"security",...}}\\n\\n

Each frame ends with a double newline. The client's EventSource API fires
an `onmessage` callback for each frame.

HOW EVENTS GET INTO THE QUEUE:
--------------------------------
_run_review() calls job_store.push_event() after each node completes and on
job terminal transitions. The SSE endpoint polls job.events from an offset,
yielding each new event as it appears. A 15-second heartbeat keeps the
connection alive through proxies that close idle connections.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from app.api.schemas import (
    AgentProgress,
    HealthResponse,
    ReviewStatusResponse,
    ReviewSubmitResponse,
)
from app.config import settings
from app.graph.build_graph import TRACKABLE_NODES, build_review_graph
from app.jobs.models import JobRecord, JobStatus
from app.jobs.store import job_store
from app.sandbox.extractor import validate_and_extract
from app.tools.tool_manager import ToolManager

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
# GET /review/{job_id}/stream  — M6: SSE streaming
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/review/{job_id}/stream")
async def stream_review(job_id: str):
    """
    Subscribe to real-time events for a review job via Server-Sent Events.

    The response is a persistent `text/event-stream`. The server pushes one
    SSE frame for each event as agents complete:

      node_complete  — one agent node finished; includes progress counts
      job_complete   — the review is done; data contains the full report
      job_failed     — the review failed; data contains the error message
      heartbeat      — sent every 15 s to keep the connection alive

    The stream closes automatically when the job reaches a terminal state
    (complete or failed).

    HOW TO CONSUME (browser):
        const es = new EventSource('/review/<id>/stream');
        es.onmessage = e => {
            const evt = JSON.parse(e.data);
            if (evt.type === 'job_complete') showReport(evt.data);
        };

    HOW TO CONSUME (curl):
        curl -N http://localhost:8000/review/<id>/stream
    """
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    async def event_generator():
        """
        Async generator that yields SSE-formatted strings.

        Polls job.events from `offset` forward, yielding each new event.
        Falls back to a heartbeat every 15 s while waiting for new events.
        Closes when the job reaches a terminal state AND all events are sent.
        """
        POLL_INTERVAL = 0.5     # seconds between event-queue checks
        HEARTBEAT_EVERY = 15    # seconds between heartbeat frames
        offset = 0              # index into job.events — only send new events
        last_heartbeat = asyncio.get_event_loop().time()

        TERMINAL = {JobStatus.COMPLETE, JobStatus.FAILED}

        while True:
            current_job = job_store.get(job_id)
            if not current_job:
                break

            # ── Drain any new events since last poll ──────────────────────────
            new_events = current_job.events[offset:]
            for event in new_events:
                payload = json.dumps(event)
                yield f"data: {payload}\n\n"
                offset += 1

            # ── Close stream once terminal and all events drained ─────────────
            if current_job.status in TERMINAL and offset >= len(current_job.events):
                break

            # ── Heartbeat — prevent proxy / load balancer from closing idle conn
            now = asyncio.get_event_loop().time()
            if now - last_heartbeat >= HEARTBEAT_EVERY:
                ts = datetime.now(timezone.utc).isoformat()
                hb = json.dumps({"type": "heartbeat", "data": {}, "ts": ts})
                yield f"data: {hb}\n\n"
                last_heartbeat = now

            await asyncio.sleep(POLL_INTERVAL)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            # Disable buffering on proxies/nginx so frames arrive immediately
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


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

    Called by FastAPI's BackgroundTasks AFTER the 202 response is sent. It:
      1. Creates a ToolManager (starts three MCP server subprocesses)
      2. Gets per-agent tool lists from the ToolManager
      3. Builds a job-specific compiled graph with tools baked in via closures
      4. Streams the graph via astream(), updating job progress as nodes complete
      5. Writes the final report when done
      6. Cleans up the ToolManager (shuts down MCP subprocesses) and the temp dir

    HOW ToolManager FITS HERE:
    ---------------------------
    ToolManager is an async context manager (async with ... as tm).
    It starts three subprocesses when you enter it (git_reader, linter, test_runner)
    and stops them when you exit. Using `async with` guarantees cleanup even if
    an exception is raised anywhere inside the block.

    We build the graph INSIDE the `async with` block because the compiled graph's
    node closures hold references to the tool objects. Those tool objects make async
    calls to the MCP subprocesses — so the subprocesses must still be running when
    the nodes execute. Exiting the `async with` block BEFORE the graph finishes
    would kill the subprocesses mid-run.

    HOW astream() WORKS:
    ---------------------
    graph.astream(initial_state) is an async generator.
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
        # Agent output fields start as None — nodes fill them in
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
        # ── Start MCP server subprocesses and get tool objects ─────────────────
        # `async with` ensures subprocesses are stopped when the block exits,
        # even if an exception is raised. This is critical — orphaned subprocesses
        # would pile up over many review jobs.
        async with ToolManager(extraction.repo_dir) as tm:
            git_tools = tm.git_reader_tools()
            linter_tools = tm.linter_tools()
            test_tools = tm.test_runner_tools()

            # ── Build a job-specific graph with tools in node closures ─────────
            # Each call to build_review_graph() compiles a fresh graph with the
            # current job's tool objects captured in the node closure functions.
            graph = build_review_graph(
                git_tools=git_tools,
                linter_tools=linter_tools,
                test_tools=test_tools,
            )

            # ── Stream node completion events ──────────────────────────────────
            async for event in graph.astream(initial_state):
                for node_name, node_output in event.items():
                    if node_name not in TRACKABLE_NODES:
                        continue  # skip LangGraph internal bookkeeping events

                    logger.info("Node '%s' completed for job %s", node_name, job_id)

                    # Update job progress in the in-memory store
                    job = job_store.get(job_id)
                    if job:
                        if node_name in job.pending_agents:
                            job.pending_agents.remove(node_name)
                        if node_name not in job.completed_agents:
                            job.completed_agents.append(node_name)
                        # Accumulate each node's output dict into agent_results
                        job.agent_results.update(node_output)

                    # M6: push SSE event so streaming clients see immediate progress
                    job_store.push_event(
                        job_id,
                        "node_complete",
                        {
                            "node": node_name,
                            "completed_agents": list(job.completed_agents) if job else [],
                            "pending_agents":   list(job.pending_agents)   if job else [],
                        },
                    )

            # ── Commit job status INSIDE the `async with` block ───────────────
            # IMPORTANT: We update the job status here (before ToolManager.__aexit__
            # runs) because anyio subprocess cleanup can raise CancelledError during
            # __aexit__, which would skip any code placed AFTER the `async with` block.
            # Committing status here guarantees it's written even if cleanup fails.
            job = job_store.get(job_id)
            final_report = (job.agent_results.get("final_report") if job else None)

            if final_report:
                job_store.update_status(job_id, JobStatus.COMPLETE, report=final_report)
                logger.info("Job %s completed successfully", job_id)
                # M6: push terminal event so SSE clients see the full report
                job_store.push_event(job_id, "job_complete", final_report)
            else:
                err_msg = "Review pipeline completed but produced no report"
                job_store.update_status(
                    job_id,
                    JobStatus.FAILED,
                    error=err_msg,
                )
                logger.error("Job %s: no final report produced", job_id)
                # M6: push failure event
                job_store.push_event(job_id, "job_failed", {"error": err_msg})

        # ── ToolManager exited — MCP subprocesses are now stopped ─────────────
        # (status was already committed inside the `async with` block above)

    except BaseException as exc:
        # Catch BaseException (not just Exception) to handle asyncio.CancelledError,
        # which anyio can raise during MCP subprocess cleanup in ToolManager.__aexit__.
        # If the graph already completed and we already committed status above, this
        # is a no-op. We only update status here if the graph itself failed.
        job = job_store.get(job_id)
        if job and job.status == JobStatus.RUNNING:
            logger.exception("Job %s failed with unexpected error: %s", job_id, exc)
            job_store.update_status(job_id, JobStatus.FAILED, error=str(exc))
            # M6: push failure event so SSE clients aren't left hanging
            job_store.push_event(job_id, "job_failed", {"error": str(exc)})
        else:
            # Graph completed; error is from cleanup only — log and move on
            logger.debug(
                "Job %s cleanup raised %s (job already %s — ignoring)",
                job_id,
                type(exc).__name__,
                job.status if job else "unknown",
            )

    finally:
        # Always clean up the extracted repo temp directory, even on failure.
        # ToolManager cleanup happens via `async with` above — this only cleans
        # the extracted ZIP contents on disk.
        logger.info("Cleaning up temp dir for job %s", job_id)
        extraction.cleanup()
