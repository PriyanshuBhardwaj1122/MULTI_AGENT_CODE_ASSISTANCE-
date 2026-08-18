"""
agents/summary.py — LLM-powered executive summary (M5).

WHAT THIS MODULE DOES:
    The summary_node in graph/nodes.py calls two things from here:

    1. deduplicate_findings(findings_by_category)
       Pure Python. Removes duplicate issues that multiple agents reported
       for the same file+line+category. Keeps the highest-severity version.

    2. generate_executive_summary(all_findings, score, repo_name)
       LLM call (no tools). Given the complete deduplicated findings list,
       asks Claude to write a 2-3 sentence executive summary, identify the
       top 3 most important issues, and give one primary recommendation.

WHY A SEPARATE LLM CALL FOR THE SUMMARY?
    The summary_node has no MCP tools — it only reads from graph state.
    But state contains raw findings dicts, not human prose. A pure-Python
    summary ("3 issues found in 2 categories") is weak. A brief LLM call
    with the actual findings produces something actionable:
    "The codebase has a critical SQL injection risk on line 12 of db.py and
    two unused imports. The SQL issue should be fixed before deploying."

SCORING FORMULA (M5 — improved over M3):
    Start at 100. Deduct per finding:
      critical: -25   (one critical = 75/100)
      high:     -15
      medium:   -8
      low:      -2
    Clamp to [0, 100].

    Rationale: a single critical issue is a showstopper regardless of how
    many low-severity issues exist. The formula reflects that.
"""
import logging
from typing import Optional

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.config import settings

logger = logging.getLogger(__name__)

SCORE_WEIGHTS = {
    "critical": 25,
    "high":     15,
    "medium":   8,
    "low":      2,
}


# ─────────────────────────────────────────────────────────────────────────────
# Deduplication
# ─────────────────────────────────────────────────────────────────────────────

def deduplicate_findings(
    findings_by_category: dict[str, list[dict]],
) -> dict[str, list[dict]]:
    """
    Remove duplicate findings that multiple agents reported for the same issue.

    Two findings are considered duplicates if they share the same:
      (file, line, category)

    When duplicates exist, we keep the one with the highest severity.
    The category with the highest-severity duplicate wins.

    Returns a new dict with the same category keys but deduplicated lists.
    """
    SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}

    # Flatten all findings, tagging each with its source category
    flat: list[tuple[str, dict]] = []
    for category, findings in findings_by_category.items():
        for f in (findings or []):
            flat.append((category, f))

    # Group by (file, line, category-of-issue) — the issue's category (from Finding.category field),
    # not the agent category. This deduplicates across agents more precisely.
    seen: dict[tuple, tuple[str, dict]] = {}
    for agent_cat, f in flat:
        key = (
            f.get("file", ""),
            f.get("line"),
            f.get("category", ""),  # Finding.category field, e.g. "unused-import"
        )
        rank = SEVERITY_RANK.get(f.get("severity", "low"), 1)
        if key not in seen or rank > SEVERITY_RANK.get(seen[key][1].get("severity", "low"), 1):
            seen[key] = (agent_cat, f)

    # Rebuild by agent category
    result: dict[str, list[dict]] = {cat: [] for cat in findings_by_category}
    for agent_cat, f in seen.values():
        result[agent_cat].append(f)

    total_before = sum(len(v) for v in findings_by_category.values())
    total_after = sum(len(v) for v in result.values())
    if total_before != total_after:
        logger.info(
            "Deduplication: %d → %d findings (%d removed)",
            total_before, total_after, total_before - total_after,
        )

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Score computation
# ─────────────────────────────────────────────────────────────────────────────

def compute_score(findings_by_category: dict[str, list[dict]]) -> tuple[int, dict[str, int]]:
    """
    Compute the overall quality score and per-severity counts.

    Returns:
        (score, severity_counts)
        score: int in [0, 100]
        severity_counts: {"critical": n, "high": n, "medium": n, "low": n}
    """
    counts: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for findings in findings_by_category.values():
        for f in (findings or []):
            sev = f.get("severity", "low")
            counts[sev] = counts.get(sev, 0) + 1

    deduction = sum(SCORE_WEIGHTS.get(sev, 0) * n for sev, n in counts.items())
    score = max(0, 100 - deduction)
    return score, counts


# ─────────────────────────────────────────────────────────────────────────────
# LLM executive summary
# ─────────────────────────────────────────────────────────────────────────────

class ExecutiveSummaryOutput(BaseModel):
    """Structured output from the LLM summary call."""
    executive_summary: str = Field(
        description="2-3 sentences: what was reviewed, what was found overall, "
                    "and the most important thing to fix. No bullet points — prose only."
    )
    top_issues: list[str] = Field(
        description="The 3 most important findings, each as one short sentence. "
                    "Most severe first. Empty list if no findings."
    )
    primary_recommendation: str = Field(
        description="The single most important action the team should take next. "
                    "One sentence, concrete and specific."
    )


_SUMMARY_SYSTEM = """You are a senior engineering lead writing a brief code review executive summary.
Your audience is a team lead who needs to understand the risk level quickly and know what to fix first.
Be direct, specific, and actionable. Reference actual files and issues from the findings.
If there are no findings, say so clearly and note that the code passed automated review."""


async def generate_executive_summary(
    findings_by_category: dict[str, list[dict]],
    score: int,
    repo_name: str,
    agent_errors: dict[str, str],
) -> Optional[ExecutiveSummaryOutput]:
    """
    Call the LLM to produce a human-readable executive summary.

    Returns None on any LLM error — the caller falls back to a plain-text summary.
    """
    all_findings = [
        f for findings in findings_by_category.values() for f in (findings or [])
    ]

    if not all_findings and not agent_errors:
        # Fast path: no LLM call needed for a clean repo
        return ExecutiveSummaryOutput(
            executive_summary=f"{repo_name} passed automated review with no issues found across static analysis, security, performance, and style checks.",
            top_issues=[],
            primary_recommendation="No action required — continue maintaining current code quality standards.",
        )

    # Build a compact findings digest for the LLM prompt
    findings_text = _format_findings_for_prompt(findings_by_category, agent_errors)

    prompt = f"""Repository: {repo_name}
Overall score: {score}/100

FINDINGS:
{findings_text}

Write the executive summary based on the findings above."""

    try:
        llm = ChatAnthropic(
            model=settings.model_name,
            api_key=settings.anthropic_api_key,
            temperature=0,
            max_tokens=1024,
        )
        structured_llm = llm.with_structured_output(ExecutiveSummaryOutput)
        result: ExecutiveSummaryOutput = await structured_llm.ainvoke([
            SystemMessage(content=_SUMMARY_SYSTEM),
            HumanMessage(content=prompt),
        ])
        return result
    except Exception as exc:
        logger.error("Executive summary LLM call failed: %s", exc)
        return None


def _format_findings_for_prompt(
    findings_by_category: dict[str, list[dict]],
    agent_errors: dict[str, str],
) -> str:
    """Format findings compactly for the LLM prompt. Truncates if very long."""
    lines: list[str] = []

    for category, findings in findings_by_category.items():
        if not findings:
            continue
        lines.append(f"\n[{category.upper()}]")
        for f in findings[:10]:  # cap at 10 per category for prompt size
            sev = f.get("severity", "low").upper()
            file_ = f.get("file", "?")
            line_ = f.get("line")
            msg = f.get("message", "")
            loc = f"{file_}:{line_}" if line_ else file_
            lines.append(f"  {sev} — {loc}: {msg}")

    for agent, error in (agent_errors or {}).items():
        lines.append(f"\n[{agent.upper()} — AGENT ERROR]: {error[:120]}")

    return "\n".join(lines) if lines else "No findings."
