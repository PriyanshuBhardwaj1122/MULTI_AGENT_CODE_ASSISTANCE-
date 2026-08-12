"""
agents/base.py — Shared LangChain agent factory.

STATUS: STUB — implemented in M3.

WHAT WILL THIS DO IN M3?
--------------------------
Every agent (static_analysis, security, performance, style) is built the same way:
  1. A system prompt describing what this agent looks for
  2. The Claude LLM via LangChain's ChatAnthropic
  3. A set of MCP-backed LangChain Tools bound to the LLM
  4. A structured output parser that forces the LLM's output to match
     our Finding schema (file, line, severity, message, evidence, suggestion)

This factory function produces that combination so each agent module doesn't
repeat the wiring:

    def build_agent(system_prompt: str, tools: list[BaseTool]) -> Runnable:
        llm = ChatAnthropic(model=settings.MODEL_NAME, api_key=settings.ANTHROPIC_API_KEY)
        llm_with_tools = llm.bind_tools(tools)
        parser = PydanticOutputParser(pydantic_object=AgentOutput)
        prompt = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            ("human", "{input}"),
        ])
        return prompt | llm_with_tools | parser

WHY bind_tools() AND NOT HARDCODING TOOL CALLS?
------------------------------------------------
llm.bind_tools(tools) tells Claude which tools exist and what their signatures are.
Claude then decides ON ITS OWN when to call a tool, with what arguments, and how
to interpret the result — before producing its Finding output. This is what makes
it a "tool-using agent" rather than just a code-calling LLM.

WHY A STRUCTURED OUTPUT PARSER?
---------------------------------
Without it, the LLM returns free-form text. With PydanticOutputParser bound to
our Finding schema, if the LLM's response doesn't match the schema, LangChain's
OutputFixingParser automatically re-prompts with the validation error attached.
This is our main guarantee that every finding has file/line/severity/evidence.
"""
# Implemented in M3. Stub file created now to establish the module structure.
