"""
agents/base.py — Shared LangChain agent factory + Finding/AgentOutput schemas.

HOW THE AGENTS IN THIS APP WORK — TWO PHASES:
===============================================

Phase 1: Agentic tool-calling loop
    Claude receives the system prompt, the analysis task, and a list of
    available tools (list_files, read_file, run_linter, run_tests).
    It autonomously decides which tools to call and in what order.
    After each tool call, the result is appended to the conversation.
    This continues until Claude stops requesting tool calls.

    Example loop for Static Analysis:
      Claude → "I'll list the files first"
      list_files() → ["src/app.py", "tests/test_app.py"]
      Claude → "I'll run the linter on the whole repo"
      run_linter(language="python") → {"findings": [{"rule": "F401", ...}]}
      Claude → "I'll read app.py since it has linter hits"
      read_file("src/app.py") → "import os\nimport sys\ndef calculate..."
      Claude → "Okay, I have enough. Here is my analysis..."

Phase 2: Structured extraction
    Once the loop ends, we send the full conversation to Claude again — but
    this time WITHOUT tools and WITH with_structured_output(AgentOutput).
    This forces the response to be valid JSON matching our Finding schema.

    WHY NOT COMBINE PHASES 1 AND 2?
    Claude's tool-use feature and structured-output feature both work via the
    same underlying mechanism (tool definitions). Combining them in one call
    leads to ambiguity: should Claude call a tool or return structured JSON?
    Separating phases avoids this entirely.

ANTI-HALLUCINATION:
    The Finding schema has a required `evidence` field: rule IDs, CWE numbers,
    or quoted code snippets. Claude can't produce a valid finding without it.
    This is enforced at the schema level, not by prompting alone.
"""
import json
import logging
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from app.config import settings

logger = logging.getLogger(__name__)

MAX_TOOL_RESULT_CHARS = 3000  # Truncate large tool results before the extraction step


# ─────────────────────────────────────────────────────────────────────────────
# Output schema — every agent produces one AgentOutput
# ─────────────────────────────────────────────────────────────────────────────

class Finding(BaseModel):
    """
    A single code issue found by an analysis agent.

    ALL FIELDS are required (except line which can be null for repo-wide issues).
    The `evidence` field is the anti-hallucination guard: Claude must cite
    something concrete — a rule ID, CWE number, or code snippet.
    """
    file: str = Field(
        description="Relative path to the file (e.g. 'src/app.py'). "
                    "Use 'repo' for repo-wide findings not tied to a specific file."
    )
    line: int | None = Field(
        None,
        description="1-indexed line number of the issue, or null if not line-specific."
    )
    severity: str = Field(
        description="One of: 'low', 'medium', 'high', 'critical'. "
                    "critical = crashes/data corruption. high = definite wrong behavior. "
                    "medium = probable bug. low = minor / cleanup."
    )
    category: str = Field(
        description="Short label for the issue type, e.g. 'unused-import', "
                    "'hardcoded-secret', 'n-plus-one', 'missing-error-handling'."
    )
    message: str = Field(
        description="Clear, specific explanation of the issue. "
                    "Bad: 'Bad code'. Good: 'Variable x is assigned on line 5 but never read'."
    )
    evidence: str = Field(
        description="REQUIRED proof. Use one of: "
                    "(1) ruff rule code — e.g. 'F401: os imported but unused', "
                    "(2) CWE ID — e.g. 'CWE-89: SQL Injection', "
                    "(3) exact code snippet — e.g. 'password = \"super_secret_123\"'. "
                    "NEVER fabricate evidence. If you have none, omit the finding."
    )
    suggestion: str = Field(
        description="Concrete, actionable fix recommendation."
    )


class AgentOutput(BaseModel):
    """The complete structured output from one analysis agent run."""
    findings: list[Finding] = Field(
        default_factory=list,
        description="All issues found. Empty list if none."
    )
    files_analyzed: int = Field(
        0,
        description="Number of files the agent read during its analysis."
    )
    summary: str = Field(
        description="One-sentence summary, e.g. "
                    "'Found 3 unused imports and 1 likely bug in the calculate() function'."
    )


# ─────────────────────────────────────────────────────────────────────────────
# LLM constructor
# ─────────────────────────────────────────────────────────────────────────────

def _make_llm(temperature: float = 0) -> ChatAnthropic:
    """
    Create a ChatAnthropic instance from app settings.

    temperature=0 means deterministic output — same input produces same output.
    For analysis agents we want consistency, not creativity.
    """
    return ChatAnthropic(
        model=settings.model_name,
        api_key=settings.anthropic_api_key,
        temperature=temperature,
        max_tokens=4096,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Core agent runner
# ─────────────────────────────────────────────────────────────────────────────

async def run_agent(
    system_prompt: str,
    human_message: str,
    tools: list[BaseTool],
    max_iterations: int = 8,
) -> AgentOutput:
    """
    Run a tool-using LangChain agent to completion, return structured output.

    Args:
        system_prompt:   The agent's persona, focus area, and rules.
        human_message:   The specific task for this run (repo name, languages, instructions).
        tools:           LangChain StructuredTool objects from ToolManager.
        max_iterations:  Safety cap — stops the loop even if Claude keeps calling tools.
                         8 iterations is plenty for: list_files + run_linter + read 3-4 files.

    Returns:
        AgentOutput with the findings list and metadata.
        On any unrecoverable error, returns an empty AgentOutput with the error in summary.
    """
    llm_with_tools = _make_llm().bind_tools(tools)

    # ── Phase 1: Agentic tool-calling loop ───────────────────────────────────
    # We build up a message list, alternating between Claude responses and
    # tool results. LangChain's message types map directly to the Claude API's
    # message roles: SystemMessage=system, HumanMessage=user, AIMessage=assistant,
    # ToolMessage=tool_result.
    messages: list[Any] = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=human_message),
    ]

    for iteration in range(max_iterations):
        logger.debug("Agent loop iteration %d/%d", iteration + 1, max_iterations)

        # Send messages to Claude — it may respond with text AND/OR tool_calls
        response: AIMessage = await llm_with_tools.ainvoke(messages)
        messages.append(response)

        # If Claude returned no tool calls, it's done thinking
        if not response.tool_calls:
            logger.debug("Agent finished after %d iteration(s)", iteration + 1)
            break

        # Execute every tool call Claude requested, append results
        for tc in response.tool_calls:
            tool_name: str = tc["name"]
            tool_args: dict = tc["args"]
            tool_call_id: str = tc["id"]

            logger.debug("Calling tool: %s(%s)", tool_name, list(tool_args.keys()))

            # Find the tool by name
            matched = next((t for t in tools if t.name == tool_name), None)
            if matched is None:
                tool_result = f"ERROR: tool '{tool_name}' not found in available tools"
            else:
                try:
                    # ainvoke is the async version of invoke — works for both
                    # sync and async tools because LangChain wraps them uniformly
                    tool_result = await matched.ainvoke(tool_args)
                except Exception as exc:
                    tool_result = f"ERROR running {tool_name}: {exc}"

            # ToolMessage links the result back to the specific tool call via id
            messages.append(
                ToolMessage(content=str(tool_result), tool_call_id=tool_call_id)
            )
    else:
        # Loop hit max_iterations without Claude stopping — log a warning
        logger.warning(
            "Agent hit max_iterations (%d) — forcing structured extraction now",
            max_iterations,
        )

    # ── Phase 2: Structured extraction ───────────────────────────────────────
    # Build a readable summary of the conversation (truncating big tool results)
    # so Claude can synthesize its findings into the AgentOutput schema.
    conversation_text = _format_conversation(messages)

    extraction_prompt = (
        "You have completed your analysis. Based on the tool calls and results above, "
        "produce a structured summary of all findings.\n\n"
        f"CONVERSATION:\n{conversation_text}\n\n"
        "RULES:\n"
        "- Include ONLY issues you found with concrete evidence from the tool output above\n"
        "- Do NOT invent findings not supported by actual tool output\n"
        "- Count the files you actually read for files_analyzed\n"
        "- Write a one-sentence summary field describing overall findings\n"
        "- If no issues were found, return an empty findings list and say so in summary"
    )

    structured_llm = _make_llm().with_structured_output(AgentOutput)

    try:
        output: AgentOutput = await structured_llm.ainvoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=extraction_prompt),
        ])
        logger.info(
            "Agent complete: %d findings, %d files analyzed",
            len(output.findings),
            output.files_analyzed,
        )
        return output

    except Exception as exc:
        logger.error("Structured extraction failed: %s", exc)
        return AgentOutput(
            findings=[],
            files_analyzed=0,
            summary=f"Agent ran but structured extraction failed: {exc}",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _format_conversation(messages: list[Any]) -> str:
    """
    Convert the message list from Phase 1 into a readable text block
    for the Phase 2 extraction prompt.

    We truncate long tool results so the extraction context doesn't explode.
    System messages are skipped — they'll be re-provided in the extraction call.
    """
    parts: list[str] = []

    for msg in messages:
        if isinstance(msg, SystemMessage):
            continue  # will be re-provided fresh

        elif isinstance(msg, HumanMessage):
            parts.append(f"[USER]\n{msg.content}")

        elif isinstance(msg, AIMessage):
            text_parts: list[str] = []
            if msg.content:
                text_parts.append(str(msg.content))
            for tc in (msg.tool_calls or []):
                args_str = json.dumps(tc["args"], indent=2)
                text_parts.append(f"<tool_call name={tc['name']}>\n{args_str}\n</tool_call>")
            parts.append("[ASSISTANT]\n" + "\n".join(text_parts))

        elif isinstance(msg, ToolMessage):
            content = str(msg.content)
            if len(content) > MAX_TOOL_RESULT_CHARS:
                content = (
                    content[:MAX_TOOL_RESULT_CHARS]
                    + f"\n... [{len(content) - MAX_TOOL_RESULT_CHARS} chars truncated]"
                )
            parts.append(f"[TOOL RESULT]\n{content}")

    return "\n\n".join(parts)


def agent_output_to_dicts(output: AgentOutput) -> list[dict]:
    """
    Convert AgentOutput.findings (list of Finding Pydantic objects) into
    plain dicts that summary_node and the report schema can handle.

    Called by each node after run_agent() returns.
    """
    return [f.model_dump() for f in output.findings]
