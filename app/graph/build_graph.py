"""
graph/build_graph.py — Assembles and compiles the LangGraph StateGraph.

THIS IS THE HEART OF THE ORCHESTRATION LAYER.

WHY LANGGRAPH OVER PLAIN asyncio.gather()?
-------------------------------------------
asyncio.gather() can run coroutines in parallel, but it gives you none of:
  • A shared typed state object that flows between steps
  • Automatic fan-in (waiting for all parallel nodes before running the next)
  • Node-level failure isolation (one crash doesn't kill the whole gather)
  • Streaming progress events (which node just finished?)
  • Built-in checkpointing/persistence (v2 feature)

LangGraph's StateGraph handles all of this. You declare a graph of nodes
and edges, compile it once, then invoke it per request — the framework
manages the parallelism, merging, and event streaming for you.

HOW THE GRAPH LOOKS:
--------------------
         ┌─────────────────┐
         │      START      │
         └────────┬────────┘
          ┌───────┼───────┬───────────┐
          ▼       ▼       ▼           ▼
       static  security perf       style
       analysis
          └───────┼───────┴───────────┘
                  ▼
               summary
                  │
                  ▼
               END

The four analysis nodes fan out from START — LangGraph runs any node
whose input dependencies are satisfied. All four fire immediately and run
concurrently (as Python async tasks on the same event loop).

The summary node has four incoming edges — it won't start until ALL four
analysis nodes have written their output into state. This is the automatic
fan-in. LangGraph handles this based purely on the edge definitions.

TWO WAYS TO GET A COMPILED GRAPH:
-----------------------------------
1. review_graph (module-level singleton)
   Built once at import time with NO tools (stub nodes that sleep 50ms).
   Used for: unit tests, health checks, backward compat.

2. build_review_graph(git_tools, linter_tools, test_tools) factory
   Called per-job by _run_review() in routes.py. Creates wrapper closures
   so each node receives the job-specific ToolManager tools.
   Used for: all real review runs.

THE CLOSURE PATTERN FOR TOOLS:
--------------------------------
LangGraph requires nodes to have the signature:  async def node(state) -> dict
We can't add a `tools` parameter without fighting LangGraph's internals.

Instead, for each job we create thin wrapper closures:

    async def sa_node(state: ReviewState) -> dict:
        return await static_analysis_node(state, sa_tools)

    graph.add_node("static_analysis", sa_node)

`sa_node` satisfies LangGraph's interface. It closes over `sa_tools` from
the outer function scope — this is how tools get into nodes without changing
the node signature. Python captures the variable by reference at closure
creation time, so when `sa_node` runs, it reads the current value of `sa_tools`.

This is efficient: all four parallel nodes share the same ToolManager's
tool objects, which hold live MCP subprocess connections.
"""
import logging
from typing import Sequence

from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph

from app.graph.nodes import (
    performance_node,
    security_node,
    static_analysis_node,
    style_node,
    summary_node,
)
from app.graph.state import ReviewState

logger = logging.getLogger(__name__)


def build_review_graph(
    git_tools: Sequence[BaseTool] | None = None,
    linter_tools: Sequence[BaseTool] | None = None,
    test_tools: Sequence[BaseTool] | None = None,
):
    """
    Compile a LangGraph review graph, optionally with real tools injected into nodes.

    When called with no arguments (or all-None), every analysis node runs as a stub
    (returns empty findings immediately). This is the pre-compiled default used in tests.

    When called with tool lists from ToolManager, the Static Analysis node gets real
    tools and runs the actual LangChain agent. The other three nodes (security,
    performance, style) remain stubs until M4.

    Args:
        git_tools:    list_files / read_file / get_commit_history tools
        linter_tools: run_linter tool
        test_tools:   run_tests tool

    Returns:
        A compiled LangGraph CompiledStateGraph ready for .astream() calls.
    """
    # ── Tool lists per agent (what each agent is allowed to use) ────────────────
    # Static Analysis: needs all tools — read code, lint, run tests
    sa_tools = list(git_tools or []) + list(linter_tools or []) + list(test_tools or [])

    # Security (M4): git reader for secret scanning + dependency files
    sec_tools = list(git_tools or [])

    # Performance (M4): git reader to spot N+1, blocking I/O, etc.
    perf_tools = list(git_tools or [])

    # Style (M4): linter only — style is nearly 100% linter-driven
    style_tools = list(linter_tools or [])

    # ── Create node closures that capture their tool lists ───────────────────────
    # Each closure satisfies LangGraph's (state) -> dict signature while
    # secretly forwarding `tools` to the underlying node implementation.

    async def sa_node(state: ReviewState) -> dict:
        """Static Analysis node with injected tools."""
        return await static_analysis_node(state, sa_tools or None)

    async def sec_node(state: ReviewState) -> dict:
        """Security node with injected tools."""
        return await security_node(state, sec_tools or None)

    async def perf_node(state: ReviewState) -> dict:
        """Performance node with injected tools."""
        return await performance_node(state, perf_tools or None)

    async def style_node_wrapper(state: ReviewState) -> dict:
        """Style node with injected tools."""
        return await style_node(state, style_tools or None)

    # ── Build and compile the graph ──────────────────────────────────────────────
    graph = StateGraph(ReviewState)

    # Register nodes by name — these names appear in the astream() event dict
    # and in the progress tracking in routes.py.
    graph.add_node("static_analysis", sa_node)
    graph.add_node("security",        sec_node)
    graph.add_node("performance",     perf_node)
    graph.add_node("style",           style_node_wrapper)
    graph.add_node("summary",         summary_node)  # no tools needed — reads state only

    # ── Sequential pipeline (avoids concurrent API rate limits on free tier) ───────
    # START → static_analysis → security → performance → style → summary → END
    # Each agent runs only after the previous one finishes, so only one LLM
    # call is in flight at a time. Slower than parallel but reliable on any API tier.
    graph.add_edge(START,             "static_analysis")
    graph.add_edge("static_analysis", "security")
    graph.add_edge("security",        "performance")
    graph.add_edge("performance",     "style")
    graph.add_edge("style",           "summary")

    # ── Terminal edge ────────────────────────────────────────────────────────────
    graph.add_edge("summary", END)

    compiled = graph.compile()
    logger.info(
        "Review graph compiled (tools: git=%d, linter=%d, test=%d)",
        len(git_tools or []),
        len(linter_tools or []),
        len(test_tools or []),
    )
    return compiled


# ── Module-level stub graph ─────────────────────────────────────────────────────
# Built once at import time with NO tools.
# Used by: unit tests, test_mcp_servers.py, backward-compat imports.
# Real review runs call build_review_graph(git_tools, linter_tools, test_tools)
# inside _run_review() in routes.py.
review_graph = build_review_graph()

# The node names we emit progress events for (matches what astream() yields as keys)
TRACKABLE_NODES = {"static_analysis", "security", "performance", "style", "summary"}
