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
whose input dependencies are satisfied. START has no dependencies, so all
four fire immediately and run concurrently.

The summary node has four incoming edges — it won't start until ALL four
of the analysis nodes have written their output into state. This is the
fan-in. LangGraph handles this automatically based on the edge definitions.

HOW DOES LangGraph KNOW WHICH EDGES ARE PARALLEL?
---------------------------------------------------
It doesn't explicitly — it just runs any node whose dependencies are met.
When you add_edge(START, "static_analysis") and add_edge(START, "security"),
both nodes depend only on START (which is always immediately satisfied), so
both are scheduled as soon as the graph begins. If your hardware has multiple
cores (or if the nodes are async and yield to the event loop), they run
concurrently.

COMPILED GRAPH:
---------------
graph.compile() returns a Runnable — an object with .invoke() and .astream()
methods. We call compile() ONCE at module import time. Every request reuses
the same compiled graph object; each invocation gets its own state dict.
"""
import logging

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


def build_review_graph():
    """
    Define nodes and edges, then compile.

    Call this once at startup. The returned compiled graph is stateless —
    all request-specific data lives in the state dict passed to .astream().
    """
    graph = StateGraph(ReviewState)

    # ── Register nodes ─────────────────────────────────────────────────────────
    # Each string name is what appears in the event stream when that node finishes.
    graph.add_node("static_analysis", static_analysis_node)
    graph.add_node("security",        security_node)
    graph.add_node("performance",     performance_node)
    graph.add_node("style",           style_node)
    graph.add_node("summary",         summary_node)

    # ── Fan-out: START → all four analysis nodes (run in parallel) ─────────────
    graph.add_edge(START, "static_analysis")
    graph.add_edge(START, "security")
    graph.add_edge(START, "performance")
    graph.add_edge(START, "style")

    # ── Fan-in: all four → summary (summary waits for all four) ───────────────
    graph.add_edge("static_analysis", "summary")
    graph.add_edge("security",        "summary")
    graph.add_edge("performance",     "summary")
    graph.add_edge("style",           "summary")

    # ── Done ───────────────────────────────────────────────────────────────────
    graph.add_edge("summary", END)

    compiled = graph.compile()
    logger.info("Review graph compiled successfully")
    return compiled


# ── Module-level singleton ──────────────────────────────────────────────────────
# Built once when this module is imported. All requests share this object.
review_graph = build_review_graph()

# The names of nodes we want to track for progress reporting
TRACKABLE_NODES = {"static_analysis", "security", "performance", "style", "summary"}
