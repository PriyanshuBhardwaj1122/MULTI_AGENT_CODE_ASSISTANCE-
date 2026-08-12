"""
graph/nodes.py — One async function per agent, wired into the LangGraph.

M1 STATUS: STUBS
-----------------
These are placeholder implementations. Each node immediately returns empty
findings after a tiny sleep (to simulate async work). This lets us verify
the whole graph wiring works end-to-end before real agents exist.

In M2 and M3 you'll replace the body of each function with a real LangChain
agent call — the function signature and return shape stay identical.

WHY ASYNC DEF?
--------------
FastAPI runs on an async event loop (uvloop, driven by uvicorn). LangGraph
supports async nodes natively. An async node can `await` LLM API calls without
blocking the event loop — meaning while Claude is thinking, Python can serve
other HTTP requests. If the nodes were regular `def` functions, each LLM call
would freeze the whole server for its duration.

WHAT DOES A NODE FUNCTION DO?
------------------------------
1. Receives the current ReviewState dict
2. Does its work (for real agents: calls tools, calls the LLM)
3. Returns a PARTIAL dict with only the keys it wants to update in state

LangGraph merges that partial dict back into the shared state. Nodes don't
return the whole state — only their additions/changes.

FAILURE PATTERN:
----------------
Each node wraps its work in try/except and on failure writes:
  {"<agent>_findings": None, "<agent>_error": "reason"}
This means the graph keeps running even if one agent fails. The Summary node
reads all four output fields and handles None gracefully.
"""
import asyncio
import logging
from typing import Optional

from app.graph.state import ReviewState

logger = logging.getLogger(__name__)


async def static_analysis_node(state: ReviewState) -> dict:
    """
    Static Analysis Agent.
    Checks: unused variables, unreachable code, type mismatches, obvious bugs.
    Tools (M2): Git repo reader, Linter (ruff/eslint), Test runner.

    STUB — returns empty findings.
    """
    try:
        logger.info("static_analysis_node: starting (job_id=%s)", state.get("job_id"))
        # M2: replace this sleep with a real LangChain agent call
        await asyncio.sleep(0.05)
        return {
            "static_analysis_findings": [],
            "static_analysis_error": None,
        }
    except Exception as exc:
        logger.error("static_analysis_node failed: %s", exc)
        return {
            "static_analysis_findings": None,
            "static_analysis_error": str(exc),
        }


async def security_node(state: ReviewState) -> dict:
    """
    Security Agent.
    Checks: hardcoded secrets, injection risks, unsafe deserialization,
            vulnerable dependencies.
    Tools (M2): Git repo reader (secret pattern scanning + dependency manifest).

    STUB — returns empty findings.
    """
    try:
        logger.info("security_node: starting (job_id=%s)", state.get("job_id"))
        await asyncio.sleep(0.05)
        return {
            "security_findings": [],
            "security_error": None,
        }
    except Exception as exc:
        logger.error("security_node failed: %s", exc)
        return {
            "security_findings": None,
            "security_error": str(exc),
        }


async def performance_node(state: ReviewState) -> dict:
    """
    Performance Agent.
    Checks: N+1 queries, blocking I/O in async code, unnecessary loops/copies,
            algorithmic complexity concerns.
    Tools (M2): Git repo reader. Test runner deferred to v2.

    STUB — returns empty findings.
    """
    try:
        logger.info("performance_node: starting (job_id=%s)", state.get("job_id"))
        await asyncio.sleep(0.05)
        return {
            "performance_findings": [],
            "performance_error": None,
        }
    except Exception as exc:
        logger.error("performance_node failed: %s", exc)
        return {
            "performance_findings": None,
            "performance_error": str(exc),
        }


async def style_node(state: ReviewState) -> dict:
    """
    Style Agent.
    Checks: naming, formatting, docstrings, import order — against linter config
            and common conventions.
    Tools (M2): Linter only (nearly entirely linter-driven).

    STUB — returns empty findings.
    """
    try:
        logger.info("style_node: starting (job_id=%s)", state.get("job_id"))
        await asyncio.sleep(0.05)
        return {
            "style_findings": [],
            "style_error": None,
        }
    except Exception as exc:
        logger.error("style_node failed: %s", exc)
        return {
            "style_findings": None,
            "style_error": str(exc),
        }


async def summary_node(state: ReviewState) -> dict:
    """
    Summary Agent.
    Runs AFTER all four analysis nodes. Reads their outputs from state,
    deduplicates, computes overall_score, writes the executive summary.

    No MCP tools bound — its only inputs are the other four agents' outputs.
    It must handle None gracefully (an agent may have failed).

    STUB — builds a minimal placeholder report from whatever findings arrived.
    """
    try:
        logger.info("summary_node: starting (job_id=%s)", state.get("job_id"))

        # Collect findings — use empty list if an agent failed (returned None)
        findings_by_category: dict[str, list[dict]] = {
            "static_analysis": state.get("static_analysis_findings") or [],
            "security":        state.get("security_findings")        or [],
            "performance":     state.get("performance_findings")     or [],
            "style":           state.get("style_findings")           or [],
        }

        # Note which agents failed
        errors: dict[str, Optional[str]] = {
            "static_analysis": state.get("static_analysis_error"),
            "security":        state.get("security_error"),
            "performance":     state.get("performance_error"),
            "style":           state.get("style_error"),
        }
        failed_agents = [name for name, err in errors.items() if err]

        total_findings = sum(len(v) for v in findings_by_category.values())

        # Count by severity across all categories
        severity_counts: dict[str, int] = {"low": 0, "medium": 0, "high": 0, "critical": 0}
        for findings in findings_by_category.values():
            for f in findings:
                sev = f.get("severity", "low")
                severity_counts[sev] = severity_counts.get(sev, 0) + 1

        # Build executive summary text
        summary_parts = []
        if failed_agents:
            summary_parts.append(
                f"Note: the following agents did not complete — "
                f"{', '.join(failed_agents)}. Findings for those categories are incomplete."
            )
        if total_findings == 0:
            summary_parts.append("No issues found across all analyzed dimensions.")
        else:
            summary_parts.append(
                f"{total_findings} issue(s) found across "
                f"{sum(1 for v in findings_by_category.values() if v)} category/categories."
            )

        final_report = {
            # M2: real scoring logic goes here
            "overall_score": 100 if total_findings == 0 else max(0, 100 - total_findings * 5),
            "summary": " ".join(summary_parts),
            "findings": findings_by_category,
            "stats": {
                "files_analyzed": 0,  # M2: filled in from Git repo reader output
                "total_findings": total_findings,
                "findings_by_severity": severity_counts,
            },
            "agent_errors": {k: v for k, v in errors.items() if v},
        }

        return {
            "final_report": final_report,
            "summary_error": None,
        }

    except Exception as exc:
        logger.error("summary_node failed: %s", exc)
        return {
            "final_report": None,
            "summary_error": str(exc),
        }
