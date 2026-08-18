"""
graph/nodes.py — One async function per agent node, wired into the LangGraph.

NODE FUNCTION SIGNATURE:
    LangGraph calls each node as:  result = await node_fn(state)
    result must be a PARTIAL dict — only the keys this node wants to update.
    LangGraph merges it back into shared state.

HOW TOOLS GET INTO NODES — CLOSURE PATTERN:
    LangGraph requires nodes to have the signature (state) -> dict.
    build_graph.py wraps each node in a closure:

        async def sa_node(state):
            return await static_analysis_node(state, sa_tools)

    The closure captures `sa_tools` from the outer function scope.
    This is how tools get into nodes without changing LangGraph's interface.

PER-AGENT TIMEOUT (M6):
    Each agent call is wrapped in asyncio.wait_for(run_agent(...), timeout=N).
    If an agent hangs (Claude is slow, MCP subprocess stalls), the node
    times out cleanly, returns an error, and the other agents continue.
    The summary node still runs and produces a partial report.

FAILURE PATTERN:
    Every node wraps its work in try/except. On failure it writes:
        {"<agent>_findings": None, "<agent>_error": "reason"}
    The graph keeps running. summary_node handles None gracefully.
"""
import asyncio
import logging
import time
from typing import Optional

from langchain_core.tools import BaseTool

from app.config import settings
from app.graph.state import ReviewState

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Shared helper — run_agent with timeout
# ─────────────────────────────────────────────────────────────────────────────

async def _run_with_timeout(coro, timeout: int, agent_name: str):
    """
    Run an agent coroutine with a timeout.
    Returns the result or raises TimeoutError with a clear message.
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"{agent_name} agent timed out after {timeout}s — "
            "the LLM call or a tool call took too long"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Static Analysis Node — M3: real agent
# ─────────────────────────────────────────────────────────────────────────────

async def static_analysis_node(
    state: ReviewState,
    tools: list[BaseTool] | None = None,
) -> dict:
    """
    Static Analysis: logic errors, unused code, missing error handling, type mismatches.
    Tools: git_reader + linter + test_runner (all three MCP servers).
    """
    logger.info("static_analysis_node: starting (job_id=%s)", state.get("job_id"))

    if not tools:
        await asyncio.sleep(0.05)
        return {"static_analysis_findings": [], "static_analysis_error": None}

    try:
        from app.agents.base import agent_output_to_dicts, run_agent
        from app.agents.static_analysis import SYSTEM_PROMPT, build_task_message

        output = await _run_with_timeout(
            run_agent(
                system_prompt=SYSTEM_PROMPT,
                human_message=build_task_message(
                    repo_name=state.get("repo_name", "unknown"),
                    languages=state.get("detected_languages", []),
                ),
                tools=tools,
            ),
            timeout=settings.agent_timeout_seconds,
            agent_name="static_analysis",
        )

        logger.info(
            "static_analysis_node: %d findings in %d files (job=%s)",
            len(output.findings), output.files_analyzed, state.get("job_id"),
        )
        return {
            "static_analysis_findings": agent_output_to_dicts(output),
            "static_analysis_error": None,
        }

    except Exception as exc:
        logger.error("static_analysis_node failed: %s", exc, exc_info=True)
        return {"static_analysis_findings": None, "static_analysis_error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# Security Node — M4: real agent
# ─────────────────────────────────────────────────────────────────────────────

async def security_node(
    state: ReviewState,
    tools: list[BaseTool] | None = None,
) -> dict:
    """
    Security: hardcoded secrets, injection risks, unsafe deserialization,
    insecure randomness, vulnerable dependencies.
    Tools: git_reader only (reads files + dependency manifests).
    """
    logger.info("security_node: starting (job_id=%s)", state.get("job_id"))

    if not tools:
        await asyncio.sleep(0.05)
        return {"security_findings": [], "security_error": None}

    try:
        from app.agents.base import agent_output_to_dicts, run_agent
        from app.agents.security import SYSTEM_PROMPT, build_task_message

        output = await _run_with_timeout(
            run_agent(
                system_prompt=SYSTEM_PROMPT,
                human_message=build_task_message(
                    repo_name=state.get("repo_name", "unknown"),
                    languages=state.get("detected_languages", []),
                ),
                tools=tools,
            ),
            timeout=settings.agent_timeout_seconds,
            agent_name="security",
        )

        logger.info(
            "security_node: %d findings in %d files (job=%s)",
            len(output.findings), output.files_analyzed, state.get("job_id"),
        )
        return {
            "security_findings": agent_output_to_dicts(output),
            "security_error": None,
        }

    except Exception as exc:
        logger.error("security_node failed: %s", exc, exc_info=True)
        return {"security_findings": None, "security_error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# Performance Node — M4: real agent
# ─────────────────────────────────────────────────────────────────────────────

async def performance_node(
    state: ReviewState,
    tools: list[BaseTool] | None = None,
) -> dict:
    """
    Performance: N+1 queries, blocking I/O in async, unnecessary loops,
    excessive memory use, repeated computation.
    Tools: git_reader only (reads source files to spot patterns).
    """
    logger.info("performance_node: starting (job_id=%s)", state.get("job_id"))

    if not tools:
        await asyncio.sleep(0.05)
        return {"performance_findings": [], "performance_error": None}

    try:
        from app.agents.base import agent_output_to_dicts, run_agent
        from app.agents.performance import SYSTEM_PROMPT, build_task_message

        output = await _run_with_timeout(
            run_agent(
                system_prompt=SYSTEM_PROMPT,
                human_message=build_task_message(
                    repo_name=state.get("repo_name", "unknown"),
                    languages=state.get("detected_languages", []),
                ),
                tools=tools,
            ),
            timeout=settings.agent_timeout_seconds,
            agent_name="performance",
        )

        logger.info(
            "performance_node: %d findings in %d files (job=%s)",
            len(output.findings), output.files_analyzed, state.get("job_id"),
        )
        return {
            "performance_findings": agent_output_to_dicts(output),
            "performance_error": None,
        }

    except Exception as exc:
        logger.error("performance_node failed: %s", exc, exc_info=True)
        return {"performance_findings": None, "performance_error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# Style Node — M4: real agent
# ─────────────────────────────────────────────────────────────────────────────

async def style_node(
    state: ReviewState,
    tools: list[BaseTool] | None = None,
) -> dict:
    """
    Style: naming conventions, import order, missing docstrings, code complexity,
    magic numbers, formatting violations.
    Tools: linter (primary) + git_reader (for complexity and magic number analysis).
    """
    logger.info("style_node: starting (job_id=%s)", state.get("job_id"))

    if not tools:
        await asyncio.sleep(0.05)
        return {"style_findings": [], "style_error": None}

    try:
        from app.agents.base import agent_output_to_dicts, run_agent
        from app.agents.style import SYSTEM_PROMPT, build_task_message

        output = await _run_with_timeout(
            run_agent(
                system_prompt=SYSTEM_PROMPT,
                human_message=build_task_message(
                    repo_name=state.get("repo_name", "unknown"),
                    languages=state.get("detected_languages", []),
                ),
                tools=tools,
            ),
            timeout=settings.agent_timeout_seconds,
            agent_name="style",
        )

        logger.info(
            "style_node: %d findings in %d files (job=%s)",
            len(output.findings), output.files_analyzed, state.get("job_id"),
        )
        return {
            "style_findings": agent_output_to_dicts(output),
            "style_error": None,
        }

    except Exception as exc:
        logger.error("style_node failed: %s", exc, exc_info=True)
        return {"style_findings": None, "style_error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# Summary Node — M5: deduplication + LLM executive summary
# ─────────────────────────────────────────────────────────────────────────────

async def summary_node(state: ReviewState) -> dict:
    """
    Summary: runs after all four analysis nodes. Deduplicates findings,
    computes the score, calls the LLM for an executive summary.

    No MCP tools — reads only from graph state.
    Handles None gracefully (agents may have failed).

    M5 improvements over M3:
    - Cross-agent deduplication (same issue reported by two agents → keep once)
    - Improved scoring formula (critical issues penalised more heavily)
    - LLM-written executive summary (not just a string concatenation)
    """
    logger.info("summary_node: starting (job_id=%s)", state.get("job_id"))

    try:
        from app.agents.summary import (
            compute_score,
            deduplicate_findings,
            generate_executive_summary,
        )

        # ── Collect raw findings from all agents ───────────────────────────────
        raw_findings: dict[str, list[dict]] = {
            "static_analysis": state.get("static_analysis_findings") or [],
            "security":        state.get("security_findings")        or [],
            "performance":     state.get("performance_findings")     or [],
            "style":           state.get("style_findings")           or [],
        }

        agent_errors: dict[str, Optional[str]] = {
            "static_analysis": state.get("static_analysis_error"),
            "security":        state.get("security_error"),
            "performance":     state.get("performance_error"),
            "style":           state.get("style_error"),
        }
        failed_agents = {k: v for k, v in agent_errors.items() if v}

        # ── Deduplicate across agents (M5) ─────────────────────────────────────
        findings_by_category = deduplicate_findings(raw_findings)

        # ── Compute score (M5 formula) ─────────────────────────────────────────
        overall_score, severity_counts = compute_score(findings_by_category)

        # ── LLM executive summary (M5) ─────────────────────────────────────────
        # Falls back to plain text if LLM call fails (e.g. bad API key, timeout)
        llm_summary = await generate_executive_summary(
            findings_by_category=findings_by_category,
            score=overall_score,
            repo_name=state.get("repo_name", "unknown"),
            agent_errors=failed_agents,
        )

        # ── Build the final report ─────────────────────────────────────────────
        total = sum(len(v) for v in findings_by_category.values())
        analyzed_files = len({
            f["file"] for findings in findings_by_category.values()
            for f in findings if f.get("file") and f["file"] != "repo"
        })

        # Executive summary text — use LLM output or fallback
        if llm_summary:
            exec_summary = llm_summary.executive_summary
            top_issues = llm_summary.top_issues
            primary_rec = llm_summary.primary_recommendation
        else:
            sev_breakdown = ", ".join(
                f"{n} {s}" for s, n in severity_counts.items() if n > 0
            )
            exec_summary = (
                f"{total} issue(s) found ({sev_breakdown}). Score: {overall_score}/100."
                if total else "No issues found. Score: 100/100."
            )
            top_issues = []
            primary_rec = "Review the findings above and address critical/high items first."

        if failed_agents:
            exec_summary += (
                f" Note: {', '.join(failed_agents.keys())} agent(s) did not complete — "
                "their findings may be incomplete."
            )

        final_report = {
            "overall_score": overall_score,
            "executive_summary": exec_summary,
            "top_issues": top_issues,
            "primary_recommendation": primary_rec,
            "findings": findings_by_category,
            "stats": {
                "files_analyzed": analyzed_files,
                "total_findings": total,
                "findings_by_severity": severity_counts,
                "findings_by_category": {k: len(v) for k, v in findings_by_category.items()},
            },
            "agent_errors": failed_agents,
        }

        return {"final_report": final_report, "summary_error": None}

    except Exception as exc:
        logger.error("summary_node failed: %s", exc, exc_info=True)
        return {"final_report": None, "summary_error": str(exc)}
