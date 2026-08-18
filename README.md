# Multi-Agent Code Review Assistant

An automated code review system that orchestrates five specialized AI agents to analyze a repository across four quality dimensions — static analysis, security, performance, and style — then synthesizes the results into a single structured report with an overall health score.

Built with **FastAPI**, **LangGraph**, **LangChain**, **Claude (Anthropic)**, and **MCP (Model Context Protocol)**.

---

## Architecture

```
                        ┌──────────────────┐
                        │   Browser / UI   │
                        │  (index.html)    │
                        └────────┬─────────┘
                                 │ HTTP / SSE
                        ┌────────▼─────────┐
                        │   FastAPI App    │
                        │  POST /review    │
                        │  GET  /review/id │
                        │  GET  /stream    │
                        └────────┬─────────┘
                                 │ BackgroundTask
                        ┌────────▼─────────┐
                        │  LangGraph       │
                        │  StateGraph      │
                        └────────┬─────────┘
                                 │ Sequential pipeline
          ┌──────────────────────┼──────────────────────┐
          ▼                      ▼                       ▼
   Static Analysis          Security              Performance
      Agent                  Agent                  Agent
          └──────────────────────┼──────────────────────┘
                                 ▼
                            Style Agent
                                 │
                                 ▼
                          Summary Agent
                         (LLM synthesis)
```

The pipeline runs **sequentially** (static → security → performance → style → summary) to stay within API rate limits on any tier. Each agent writes findings into a shared `ReviewState` object; the summary agent reads all findings and produces the final score and executive summary.

---

## Features (all 6 milestones complete)

| Milestone | Feature |
|-----------|---------|
| M1 | FastAPI skeleton, job lifecycle (pending → running → complete/failed), in-memory store |
| M2 | Three MCP tool servers (git_reader, linter, test_runner) as stdio subprocesses |
| M3 | ToolManager async context manager; LangChain ReAct agents wired to MCP tools |
| M4 | Security, performance, and style agents — real LLM calls, structured finding output |
| M5 | Cross-agent deduplication, severity-weighted score formula, LLM executive summary |
| M6 | Server-Sent Events streaming endpoint; dark-themed drag-and-drop browser UI |

---

## Project Structure

```
code-review-assistant/
├── app/
│   ├── main.py                  # FastAPI app, static file serving
│   ├── api/
│   │   ├── routes.py            # All HTTP endpoints + SSE stream
│   │   └── schemas.py           # Pydantic response models
│   ├── graph/
│   │   ├── build_graph.py       # LangGraph StateGraph assembly
│   │   ├── nodes.py             # Five agent node implementations
│   │   └── state.py             # ReviewState TypedDict
│   ├── jobs/
│   │   ├── models.py            # JobRecord, JobStatus enum
│   │   └── store.py             # In-memory job store + push_event()
│   ├── mcp_servers/
│   │   ├── git_reader/server.py # list_files, read_file, get_commit_history
│   │   ├── linter/server.py     # run_linter (flake8)
│   │   └── test_runner/server.py# run_tests (pytest)
│   ├── tools/
│   │   └── tool_manager.py      # Async context manager for MCP connections
│   └── static/
│       └── index.html           # Full single-file browser UI
├── requirements.txt
└── README.md
```

---

## Setup

### Prerequisites

- Python 3.11+
- An Anthropic API key

### Install

```bash
git clone https://github.com/YOUR_USERNAME/code-review-assistant.git
cd code-review-assistant
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

> **Important:** The MCP servers require `mcp==1.9.4`. Version 2.0.0 removed FastMCP. Pin exactly:
> ```bash
> pip install "mcp==1.9.4"
> ```

### Configure

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

### Run

```bash
uvicorn app.main:app --reload --reload-dir app
```

Open [http://localhost:8000](http://localhost:8000) for the browser UI, or [http://localhost:8000/docs](http://localhost:8000/docs) for the interactive API docs.

---

## API Endpoints

### `POST /review`

Submit a repository ZIP for review.

```bash
curl -X POST http://localhost:8000/review \
  -F "file=@my_project.zip"
```

**Response:**
```json
{
  "job_id": "abc123",
  "status": "pending",
  "created_at": "2026-08-12T10:00:00Z"
}
```

---

### `GET /review/{job_id}`

Poll for job status and results.

```bash
curl http://localhost:8000/review/abc123
```

**Response (running):**
```json
{
  "job_id": "abc123",
  "status": "running",
  "progress": {
    "completed_agents": ["static_analysis", "security"],
    "pending_agents": ["performance", "style", "summary"]
  }
}
```

**Response (complete):**
```json
{
  "job_id": "abc123",
  "status": "complete",
  "report": { ... }
}
```

---

### `GET /review/{job_id}/stream`

Server-Sent Events stream for live progress. Connect with `EventSource` in the browser or `curl`:

```bash
curl -N http://localhost:8000/review/abc123/stream
```

**Event types:**

| `type` | `data` fields | Meaning |
|--------|--------------|---------|
| `node_complete` | `node`, `completed_agents`, `pending_agents` | One agent finished |
| `job_complete` | full report dict | All agents done |
| `job_failed` | `error: str` | Job encountered a fatal error |
| `heartbeat` | `{}` | Keep-alive ping every ~15 s |

---

### `GET /health`

```bash
curl http://localhost:8000/health
# {"status": "ok", "version": "1.0.0"}
```

---

## Report Schema

```json
{
  "job_id": "abc123",
  "status": "complete",
  "report": {
    "score": 74,
    "summary": "The codebase is in reasonable shape with 2 high-severity issues...",
    "findings": [
      {
        "category": "security",
        "severity": "high",
        "file": "app/config.py",
        "line": 12,
        "message": "Hardcoded secret detected",
        "suggestion": "Move to environment variable or secrets manager"
      }
    ],
    "findings_by_category": {
      "static_analysis": [...],
      "security": [...],
      "performance": [...],
      "style": [...]
    },
    "severity_counts": {
      "critical": 0,
      "high": 1,
      "medium": 3,
      "low": 4
    },
    "agents_completed": ["static_analysis", "security", "performance", "style", "summary"],
    "agents_failed": []
  }
}
```

---

## Scoring Formula (Judging Parameters)

The overall health score is a **severity-weighted deduction** from a perfect 100:

```
score = max(0, 100 − (critical × 25 + high × 15 + medium × 8 + low × 2))
```

| Severity | Deduction per finding | Meaning |
|----------|-----------------------|---------|
| **critical** | 25 pts | Immediate production risk — data loss, security breach, crash |
| **high** | 15 pts | Significant defect or vulnerability requiring prompt attention |
| **medium** | 8 pts | Code quality issue or potential bug worth fixing soon |
| **low** | 2 pts | Minor style, readability, or best-practice suggestion |

Score is clamped to **[0, 100]**. A project with no findings scores 100.

**Score bands displayed in the UI:**

| Range | Color | Interpretation |
|-------|-------|----------------|
| 80–100 | 🟢 Green | Healthy — ship with confidence |
| 50–79 | 🟡 Amber | Needs attention before production |
| 0–49 | 🔴 Red | Significant issues — review required |

---

## What Each Agent Checks

### Static Analysis Agent
Uses `list_files`, `read_file`, `run_linter` (flake8), and `run_tests` (pytest) tools.
- Syntax errors and import failures
- Linter violations (PEP 8, unused imports, undefined names)
- Failing or missing unit tests
- Test assertion mismatches

### Security Agent
Uses `list_files` and `read_file` tools.
- Hardcoded secrets, API keys, passwords in source files
- Dangerous function calls (`eval`, `exec`, `pickle.loads`)
- Missing input validation on user-facing parameters
- Insecure dependency patterns
- SQL injection risks, unvalidated redirects

### Performance Agent
Uses `list_files` and `read_file` tools.
- N+1 query patterns (loop + DB call)
- Blocking I/O calls inside async functions (`time.sleep`, `requests.get`)
- Unbounded loops or missing pagination
- Large object construction inside tight loops
- Missing caching on expensive repeated computations

### Style Agent
Uses `run_linter` (flake8) tool.
- PEP 8 compliance
- Missing docstrings on public functions/classes
- Overly long functions or deeply nested logic
- Inconsistent naming conventions
- Dead code (commented-out blocks, unreachable statements)

### Summary Agent
Reads the shared `ReviewState` (no tools — LLM only).
- Deduplicates findings across agents by `(file, line, category)`, keeping highest severity
- Computes the weighted score
- Produces a 2–4 sentence executive summary in natural language
- Surfaces the top 3 most critical findings for immediate attention

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| API server | FastAPI + Uvicorn |
| Orchestration | LangGraph `StateGraph` |
| Agent framework | LangChain ReAct agents |
| LLM | Anthropic Claude (via `langchain-anthropic`) |
| Tool protocol | MCP (Model Context Protocol) `mcp==1.9.4` |
| Linter tool | flake8 |
| Test tool | pytest |
| Frontend | Vanilla HTML/CSS/JS, SSE `EventSource` |
| Job store | In-memory dict (per-process) |

---

## Known Limitations

| Limitation | Details |
|-----------|---------|
| **Sequential execution** | Agents run one at a time to avoid concurrent API rate limit errors (429). On a paid API tier with higher concurrency limits, change `build_graph.py` back to parallel fan-out for ~4× speed improvement. |
| **In-memory job store** | Jobs are lost on server restart. For production, replace `store.py` with a Redis or PostgreSQL backend. |
| **Large repo context overflow** | The performance agent reads many files and can exceed the 200k-token context limit. Keep target repos under ~50 files, or reduce `max_iterations` in `nodes.py` from 8 to 3. |
| **ZIP format only** | The upload endpoint accepts ZIP archives only. Git URL support is a planned extension. |
| **Single-user, single-process** | The in-memory store is not shared across Uvicorn workers. Run with `--workers 1` (the default). |
| **MCP version pin** | Must use `mcp==1.9.4`. Version 2.0.0 removed `FastMCP`; later versions may restore it under a different import path. |

---

## License

MIT