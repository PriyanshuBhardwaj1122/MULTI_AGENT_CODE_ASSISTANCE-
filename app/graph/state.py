"""
graph/state.py — The shared state object that flows through the LangGraph.

WHAT IS GRAPH STATE?
--------------------
When you run a LangGraph, every node (agent) receives the current state dict,
does its work, and returns a *partial update* dict. LangGraph merges that
partial update into the full state before passing it to the next node.

Think of it like a relay race baton — but each runner can only write to their
own section of the baton. The Summary node at the end reads everyone's section.

WHY TypedDict AND NOT A PYDANTIC MODEL?
----------------------------------------
LangGraph's StateGraph requires TypedDict (or a compatible dict-like type).
Pydantic models are great for validation of untrusted external data, but
inside the graph we construct the state ourselves — TypedDict is enough,
and it's what the LangGraph framework expects for state-merging to work correctly.

FIELD NAMING CONVENTION:
-------------------------
<agent_name>_findings → the list of Finding dicts that agent produced (or None)
<agent_name>_error    → error message if that agent failed (or None)

Both fields exist for every agent. This way the Summary node can always check
for either: "did this agent produce findings, or did it fail?"
"""
from typing import Optional
from typing_extensions import TypedDict


class ReviewState(TypedDict, total=False):
    """
    `total=False` means all keys are optional — LangGraph nodes return partial
    dicts and the graph merges them. Without total=False, every node would need
    to return ALL fields, even the ones it doesn't touch.
    """

    # ── Inputs (set once when the graph is invoked, never changed) ─────────────
    job_id: str
    repo_path: str           # absolute path to the extracted repo on disk
    repo_name: str
    detected_languages: list[str]

    # ── Static Analysis Agent output ───────────────────────────────────────────
    static_analysis_findings: Optional[list[dict]]
    static_analysis_error: Optional[str]

    # ── Security Agent output ──────────────────────────────────────────────────
    security_findings: Optional[list[dict]]
    security_error: Optional[str]

    # ── Performance Agent output ───────────────────────────────────────────────
    performance_findings: Optional[list[dict]]
    performance_error: Optional[str]

    # ── Style Agent output ─────────────────────────────────────────────────────
    style_findings: Optional[list[dict]]
    style_error: Optional[str]

    # ── Summary Agent output (written last, after all four above) ──────────────
    final_report: Optional[dict]
    summary_error: Optional[str]
