# Roadmap: Agent Workspace (Multi-Agent Production System)

**Status:** Phase 2 in progress
**Last updated:** 2026-05-09

## Product positioning

OpenCraig is a **team knowledge management + AI work platform**:

* **Team knowledge management** — multiple users share a Library
  (folder-grant authz), each user holds folder-level reads/writes,
  the corpus is curated collectively. Phase 0–1 already shipped
  this baseline.
* **AI work platform** — agents that do the actual work (analyse
  data, transform files, generate reports) inside per-user
  sandboxes, with every step audited and every output traceable
  to its inputs. Phase 2–4 builds this layer.

This positioning shapes a few non-obvious priorities:

1. **Authz boundary is per-Library-folder, not per-tenant.** Two
   teammates with read-grants on the same `/research/` folder see
   the same content; their AGENT runs are private (per-user
   container) but the knowledge they pull from is shared.
2. **Project-level work is single-writer with read-only viewers.**
   `Project = owner-write + read-only share`; consultants can
   show clients progress without exposing edit rights. The owner
   runs agents; viewers watch. (Decision settled May 2026.)
3. **Reusable workflows are a real need.** A team building "Q3
   review" once and re-running it next quarter shouldn't have to
   re-prompt from scratch. **Skills** (Phase 5+, see below) is
   the abstraction that solves this.

This roadmap is the largest feature on the product side. It moves
us from "find the answer in your docs and cite the page" to "have
an agent do the actual work — read files, run code, write
artifacts — on your hardware, with every step audited and every
output traceable to its inputs."

The note is self-contained: read it without prior chat context.

---

## TL;DR

Today **Workspace** in OpenCraig means "the folder tree of indexed
documents". After this feature lands, we have **two** distinct
spaces:

| Surface | Purpose | Read/Write semantics |
|---|---|---|
| **Library** (renamed from current Workspace) | The indexed corpus — source of truth for retrieval | Agent has read-only access via the search tool; writes happen only through the ingestion pipeline |
| **Workspace** (NEW) | The agent's working area — where projects live and artifacts are produced | Agent has read+write access to its project's working directory; nothing here is indexed unless explicitly promoted |

A new entity — **Project** — is the container that ties together a
chat conversation, a working directory on disk, the agent runs that
have happened in it, and the artifacts produced. Each Project has
its own isolated Python execution sandbox.

The agent loop in chat is extended with three classes of tools:
**code execution** (sandboxed Python with persistent kernel),
**file I/O** in the project's workdir, and **Library bridge**
(import documents from the Library, promote artifacts back into it).

A **plan-execute-reflect** orchestrator turns a single user message
into a multi-step run with checkpointing, sub-agent fan-out for
parallel work, and human-in-the-loop gates at the dangerous
moments. Long runs survive browser close + worker restart.

This is a 3-5 month effort across 7 phases. The first 3 weeks
(Phase 0 + Phase 1) put the **Library / Workspace split** in front
of the user with no agent capabilities yet — just the architecture.
That alone is enough to demo the product direction to early
customers; the agent capabilities ship as Phase 2-6 layers on top.

---

## Why now

Three pressures converge:

1. **Customer asks have moved past Q&A.** Patent agents want to
   *produce comparison tables* between specs, not just find the
   relevant paragraphs. Biotech researchers want to *clean their
   experimental data + generate plots*, not just locate the right
   protocol. Lawyers want to *extract every obligation from a 200-
   page contract into a tracker*, not just navigate the contract.
   The current product can answer "where is X" but not "do X".
2. **The chat surface is already the obvious entry point.** Users
   land in chat to ask questions about their corpus. Telling them
   "and now copy-paste the answer into Python and clean it up
   yourself" forces a context switch we shouldn't make them eat.
3. **Self-host + sandboxed code execution is a unique combination.**
   ChatGPT Code Interpreter runs on OpenAI's machines (no good if
   your data can't leave your network). Manus runs on theirs
   (same problem). Notebook tools (Jupyter, Hex) don't have agents
   driving them. Nobody else combines (a) on-prem deployment +
   (b) a real sandboxed Python kernel + (c) an LLM agent with
   knowledge-base access. **That intersection is our wedge** for
   regulated-industry customers.

---

## Architectural decisions (with rejected alternatives)

### 1. Library / Workspace as separate top-level surfaces

**Chosen.** Two routes (`/library`, `/workspace`); two distinct
data models; two distinct file storage roots (`storage/blobs/` for
the indexed corpus, `storage/projects/<id>/` for agent workdirs).
Crossing between them is an explicit user action (Library →
Workspace = "import"; Workspace → Library = "promote to KB").

**Rejected: one unified file tree where indexing is a flag.** Was
considered briefly. Pros: simpler conceptually; users have one
place to look. Cons: agent-produced files would either accidentally
land in the index (polluting search with intermediate scratch
files) OR every agent file write needs a `do_not_index: true` flag
that gets forgotten 30% of the time. Two physical roots, with
explicit promotion, is the safer default.

**Rejected: keep current "Workspace" name for the new surface and
rename current "Workspace" → "Documents".** Was the user's first
instinct. The reason we went the other way: the current Workspace
already handles the multi-user share + folder-grant model, which
is the *Library's* concern (who can search what). Renaming current
→ Library aligns the noun with what it actually does (a curated
read-only-ish store of reference material).

### 2. Project as the container, not Conversation

**Chosen.** A Project owns: workdir on disk, single owner-writer,
artifacts, agent runs, execution sessions. A Chat Conversation
*belongs to* a Project (via FK). Multiple conversations under one
project share the same workdir + artifacts. Read-only viewers can
be added (Phase 6+ UI) but only the owner runs agents and writes
files — see "Project = owner-write + read-only share" in the
Settled decisions.

**Rejected: artifacts attached directly to Conversation.** Cleaner-
sounding but breaks the moment a user wants two conversations on
the same project ("ask the agent to clean the data" then later
"ask it to plot the cleaned data" — same workdir, different chat
threads). Project is the persistent unit; conversations come and
go.

### 3. Per-user Docker container + per-project Jupyter kernel

**Chosen.** Each *user* gets one Docker container; inside the
container we spawn one **Python kernel per project** (subprocess
under `ipykernel`). Variables, imports, opened files persist
within a kernel across `python_exec` calls. Container is reaped
after 30 min of full inactivity; kernels reaped after 10 min of
their own inactivity. Reasoning:

* For our scale (5–50 users, 1–5 active projects per user, mostly
  single-project-at-a-time work), **per-project containers were
  ~5x resource overhead with no security benefit** — the actual
  isolation boundary that matters is between *users*, not between
  projects of the same user.
* Project state isolation comes from the **kernel subprocess**
  (each `ipykernel` is a fresh Python process; no shared globals).
* Filesystem isolation: the container only bind-mounts the user's
  own project workdirs (per `Folder.shared_with` grants). Bob's
  container literally has no path to Alice's projects — kernel-
  level filesystem boundary, harder than application-level path
  filtering.

**Rejected: per-Project container.** Original design. Walked back
once we computed: 50 users × 5 projects ≈ 250 idle containers
vs ~50 with per-user. Per-user gives us project-switch in ~200 ms
(spawn a kernel subprocess) vs ~3-5s (cold-start a container).

**Rejected: per-call container.** Cold-start overhead (~2-3s per
call) wrecks the agent flow; agent runs 10-20 code blocks per
task and we'd spend more time spawning containers than running
code.

**Rejected: shared kernel pool across users.** Cross-user data
leak; obvious no.

**Rejected: bare-process subprocess sandboxing (firejail /
bubblewrap).** Linux-only; doesn't run on macOS / Windows dev
boxes; weaker than container-based isolation.

### 3b. `jupyter_client` + Docker SDK, NOT JupyterHub

**Chosen.** We manage the per-user containers ourselves via the
`docker` Python SDK. Inside each container, an `ipykernel`
subprocess hosts the project's Python state. We drive the
kernel from FastAPI via `jupyter_client.AsyncKernelManager` —
the standard Jupyter messaging library that ships connection
tracking, streaming `iopub` messages, rich `display_data`
(plots, dataframes), kernel-level interrupt + heartbeat. The
ZeroMQ-over-TCP transport works cross-container transparently
once we hand `jupyter_client` the kernel's connection-info file.

This is the path **every public LLM-code-execution system
takes** — ChatGPT Code Interpreter, E2B, AutoGen
`DockerJupyterCodeExecutor`, HuggingFace Jupyter Agent,
`vndee/llm-sandbox`, `dida.do`'s reference implementation. None
use JupyterHub.

**Rejected: JupyterHub + DockerSpawner.** Was the initial
recommendation; survey of public production deployments shows
JupyterHub is built for *humans clicking notebook UIs at scale*
(Berkeley DataHub: 1,500 students; CERN SWAN: 200 sessions/day;
NASA Pangeo, 2i2c, Bloomberg — all human-driven). Its
authentication, proxy, and spawner machinery exists to give
many simultaneous humans their own JupyterLab tab. None of that
matters when the "user" of the kernel is our orchestrator agent
on behalf of one human at a time. Adopting JupyterHub for
LLM-agent kernel management is **a path nobody has publicly
validated** for our use case, while adding an extra service +
JWT auth bridge to the deploy. The kernel-orchestration layer
JupyterHub provides is ~500 lines of `docker` SDK + `jupyter_client`
glue that we control directly.

**Rejected: bare `subprocess.run("python", "-c", code)`.** Each
call is a fresh process — variables don't persist across the
agent's multi-step work. Streaming output and matplotlib /
DataFrame rich rendering also don't survive subprocess.

**Rejected: e2b-dev open-source infra.** Better security ceiling
(Firecracker microVMs) but operationally heavy for self-host
deploys; revisit if we ever offer a public free tier.

**Reference implementation we adapt from**: AutoGen 0.4+'s
`autogen-ext.code_executors.docker_jupyter` (~600 lines, MIT)
is essentially this exact pattern. Phase 2 starts by porting
its core into `api/agent/sandbox/` and stripping the AutoGen-
specific abstractions we don't need (we keep our own agent
loop, our own audit logging, our own folder-grant filesystem
mount).

### 4. Plan-Execute-Reflect orchestration, not LangGraph

**Chosen.** A small, custom Python state machine (~500 lines)
managing `(plan, step_index, pending, done, failed, artifacts,
cost)`. Persists to DB so a worker crash mid-run resumes from
the last completed step.

**Rejected: LangGraph.** Heavy dependency, opinionated about
state shapes that don't match ours, hard to debug when graphs
get non-trivial. We need exactly enough to run a 3-stage loop
with sub-agent fan-out — a hand-rolled state machine is shorter
than the LangGraph integration would be.

**Rejected: pure ReAct without a separate planner.** The agent
loop already supports ReAct via litellm function calling. But for
multi-step research tasks, having an explicit plan the user can
review + approve before execution starts is the HITL feature
that makes the system trustworthy. ReAct alone gives users a
black box that may or may not converge.

### 5. Artifacts are first-class objects, not "files in a folder"

**Chosen.** Every agent-produced file gets an `Artifact` row:
`{id, project_id, path, mime, size, sha256, produced_by_step,
inputs: [chunk_ids, prior_artifact_ids, urls], created_at}`. The
filesystem is the primary store; the row is metadata + lineage.

**Rejected: just use the filesystem, infer everything else.**
Loses lineage. The whole point of producing artifacts in this
product (instead of letting users bring their own scripts) is the
audit trail: "this number in the xlsx came from Step 3, which
read chunks 47 and 89, which came from contract.pdf pages 12 and
15." Filesystem-only loses all of that.

### 6. Web search ships in Phase 5, not Phase 0

**Chosen.** The product can demo + sell on Library + Workspace +
sandboxed Python alone. Web search adds (a) a new external
dependency (search API key + cost), (b) a serious prompt-injection
attack surface, (c) an expectation that agents are "fully
autonomous" which mismatches our HITL ethos. Sequence it after the
core flow is stable.

**Rejected: ship web search early because every demo competitor has
it.** True but the demos are mostly cherry-picked. For our target
customers (regulated industries), defaulting to external web
fetches is a *feature negative* — many of them will turn it off.

---

## Deployment topology

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Browser                                                                  │
│   ↕ SSE                                                                  │
│ FastAPI (opencraig)                                                      │
│   ├─ chat / agent loop                                                   │
│   ├─ projects / files / library routes                                   │
│   └─ SandboxManager (~500 lines of our code)                             │
│        ├─ docker SDK: ensure_container_for_user(user_id)                 │
│        └─ jupyter_client.AsyncKernelManager (per project kernel)         │
│              ↕ ZeroMQ over TCP (kernel messaging protocol)               │
│                                                                          │
│ docker engine (host) — userns-remap enabled (root→100000+)               │
│   ├─ container alice-sandbox (per-user)                                  │
│   │    ├─ ipykernel proj_001 (subprocess; persistent state)              │
│   │    ├─ ipykernel proj_002 (subprocess; persistent state)              │
│   │    ├─ /workdir/                                                      │
│   │    │    ├─ proj_001/  ←── bind-mount → host storage/projects/        │
│   │    │    └─ proj_002/                                                 │
│   │    └─ /workspace/.envs/  ←── bind-mount → host storage/user-envs/    │
│   │         ├─ r/      (lazy-installed on first install_runtime("r"))    │
│   │         └─ julia/  (etc.)                                            │
│   └─ container bob-sandbox (per-user, fully isolated)                    │
│        ├─ ipykernel proj_007 (subprocess)                                │
│        ├─ /workdir/proj_007/                                             │
│        └─ /workspace/.envs/  ←── bob's own user-envs/, not Alice's       │
│                                                                          │
│ Host filesystem (canonical store; same data the container sees)          │
│   storage/projects/                                                      │
│     ├─ proj_001/inputs/data.csv                                          │
│     ├─ proj_001/outputs/                                                 │
│     ├─ proj_002/...                                                      │
│     └─ proj_007/...                                                      │
│   storage/user-envs/                                                     │
│     ├─ <alice_uid>/r/  (persists across container reaping)               │
│     └─ <bob_uid>/...                                                     │
└──────────────────────────────────────────────────────────────────────────┘
```

**Bind mount is the single source of truth**: the host filesystem
under `storage/projects/<id>/` IS what the container sees at
`/workdir/<id>/`. Same inodes, two paths. Workspace UI reads
files via FastAPI hitting the host filesystem directly (fast, no
docker exec round-trip, works even when container is reaped).
Agent writes via `python_exec` go into the kernel; the kernel
writes to the bind-mounted path; UI refresh shows the new file
immediately.

**Per-user mount scope**: Alice's container only mounts the
project subdirectories Alice has grants on. Bob's container has
no path that resolves to Alice's project workdir — the kernel
literally can't read it, by OS-level filesystem boundary. The
sandbox image's `write_file` tool also enforces an application-
level check that the requested path resolves inside the *current
conversation's* project workdir (preventing the agent in
proj_001 from writing into proj_002 of the same user).

**`userns-remap` for safe in-container privilege**: docker daemon
runs with `"userns-remap": "default"` (one-line `daemon.json`
change). Inside the container the agent's user is `root` and can
freely `apt install r-base`, `mamba install`, etc. — needed so
`install_runtime` works without us brokering a curated package
allowlist. On the host, that `root` is mapped to an unprivileged
uid (100000+). Bind-mount writes land owned by the remapped uid,
not host root, so FastAPI (running as the `opencraig` user) can
still read/write Workspace files without sudo. Container escapes
land in unprivileged-user space, not host root — meaningful
defense-in-depth on top of the docker isolation layer.

**Container lifecycle**:
* Cold-start happens lazily on first `python_exec` for a user
  who has no live container.
* Idle reap: container exits after 30 min with no kernel activity.
* Kernel reap: ipykernel subprocess exits after 10 min of no
  execute messages. Cheap to respawn.
* Crash recovery: dead-kernel detection via Jupyter's heartbeat
  channel; we surface "kernel died" to the agent loop, which can
  decide to retry or fail the run. Container OOM kills the
  container; FastAPI re-spawns next request.

---

## Multi-language strategy

The agent's primary code lane is Python because: (a) every data
tool we care about (pandas / pdfplumber / openpyxl / pymupdf /
matplotlib) ships first-class for Python, (b) every public LLM-
agent reference uses Python kernels, (c) our customers' data work
is overwhelmingly in Python or Python-callable. But Python
shouldn't be a *limit*: the container has a full Linux user
space, and the agent's `python_exec` can call `subprocess.run`
on anything we install.

### Phase 2 sandbox image: Python + bash + ~25 CLI tools

`opencraig/sandbox:py3.13` — base size target ~1.5 GB:

* **Python data stack**: `numpy pandas scipy scikit-learn matplotlib
  seaborn plotly duckdb pyarrow openpyxl xlsxwriter python-docx
  python-pptx pypdf pdfplumber pymupdf camelot-py pdf2image
  beautifulsoup4 lxml requests httpx jupyter ipykernel ipywidgets`
* **OS-level CLIs callable via `subprocess.run`**:
  - Office / docs: `libreoffice`, `pandoc`, `texlive-xetex`
  - PDF: `poppler-utils` (pdftotext/pdftoppm/pdfinfo), `qpdf`,
    `pdftk-java`
  - Image: `imagemagick`, `libvips`
  - Audio/video: `ffmpeg`
  - OCR: `tesseract-ocr` (with `chi_sim` for Chinese)
  - Data: `xsv`, `jq`, `dasel`, `sqlite3`, `duckdb`
  - Search/file: `ripgrep`, `fd-find`
  - Network/version: `git`, `curl`, `wget`
* **Two kernels**: Python (`ipykernel`) and Bash (`bash_kernel`).
  The agent gets `python_exec` AND `bash_exec` as separate tools
  — bash is a first-class lane for shell-flavoured workflows
  ("find files modified in last 7 days", "grep across the
  workdir", "tail a log").

### Other languages: on-demand install, not image variants

R, Julia, Node, Rust, Go, etc. are **not** baked into image
variants. Instead the agent has a first-class `install_runtime`
capability that lazily provisions language stacks the first time
they're needed, into a **per-user persistent volume** mounted at
`/workspace/.envs/`:

```
/workspace/.envs/
  r/           # IRkernel + base R + tidyverse, installed on first R use
  julia/       # IJulia + base packages
  node/        # nvm-managed Node + npm global bin
  rust/        # rustup toolchain
```

**Why this works:**

* Container runs with `userns-remap` so the agent can `apt install`
  / `mamba install` without polluting host root and without us
  brokering "which apt packages are allowed". Inside container the
  user thinks it's root; on host it maps to an unprivileged uid
  (100000+).
* `/workspace/.envs/` is a **per-user volume**, separate from the
  per-project bind-mounts. Survives container reaping. Shared
  across all of one user's projects — install R once, available
  in every project for that user.
* First R session pays a 1–3 min cold install. Every subsequent
  session, every other project of the same user: zero wait.
* Disk cost is amortized per *user* who actually needs R, not per
  *deployment*. The 90% of users who never touch R never pay.

**Agent UX:**

When the agent decides to use R (e.g. user uploaded a Bioconductor
DESeq2 study), it calls `install_runtime("r")`. The tool either
returns immediately (already installed) or streams install
progress to the chat ("Setting up R kernel for first use… ~2
min"), then registers an `r_exec` tool for the rest of the run.
Same flow for `julia`, `node`, etc.

**Settled vs custom packages:** `install_runtime` provisions the
*runtime* (R interpreter + IRkernel + a curated base set —
tidyverse for R, DataFrames+Plots for Julia). Project-specific
packages (`install.packages("DESeq2")`, `Pkg.add(...)`) are
ordinary code the agent runs inside its own kernel and persist in
the project's R/Julia env folder. We do not try to be a package
manager.

**Operator override:** an operator who *knows* their userbase is
biotech-heavy can pre-warm the user volumes by setting
`agent.runtime_preinstall: [r]` — backend installs R into each
new user's `.envs/r/` at user creation time, so even the first
session is zero-wait.

### Browser automation (Phase 5 web tooling)

`playwright` ships with `:py3.13` from Phase 5 onward —
headless Chromium, ~300 MB additional. Used by the `fetch_url`
tool when JS rendering is needed (most monitoring agency pages,
modern SaaS docs). Default disabled; `agent.web_search.enabled`
gate also gates browser launches.

---

## Observability stack

OpenCraig already emits OpenTelemetry spans for every request +
LLM call. Two additions for the agent era:

| Layer | Tool | Why |
|---|---|---|
| **LLM-call instrumentation** | `traceloop/openllmetry` (Apache-2.0) | Drop-in OTel instrumentation for `litellm` — every LLM call automatically becomes a span with `model / tokens_in / tokens_out / cost / latency / prompt_hash` attributes. No new wire protocol; rides our existing OTel collector. |
| **LLM-specific dashboard** | `langfuse/langfuse` self-host (MIT) | Accepts OTLP ingest natively; surfaces traces grouped by `agent_run_id`, token spend per user/project, prompt diffs across versions, eval queues. Replaces having to build all of this ourselves. |
| **Sandbox / kernel state** | JupyterHub-style admin panel — but built ourselves | One page in `/settings/agent-monitor` (admin only): active containers, kernels, CPU/RAM per user, active runs with current step + cost. Force-kill controls. Reads docker SDK + our `agent_runs` table. |
| **Per-project run history** | OpenCraig's own `agent_runs` + `agent_run_steps` tables | User-facing view: history of agent runs in a project, click for plan + step timeline + per-step tokens/cost/wall-time. Failure resumes from step N. "Export trace JSON" for bug reports. |

`langfuse` is operator-optional via a docker-compose profile:
operators who don't want a second observability surface can run
without it; the data still flows into the OTel collector and our
own DB.

---

## Workflow / state-machine engine

**Decision: defer Temporal/Restate; Postgres state row is
sufficient for v1.**

A row in `agent_runs` with `(plan, step_index, status,
last_checkpoint_at)` is enough to support:
* Run survives FastAPI worker restart (resume from step_index)
* Multi-minute runs that outlive an HTTP request
* Cancel / pause / approval gates (status flag transitions)

Adopt Temporal/Restate when (a) an agent run needs durable
wait > 1 minute across a process restart with a strict
correctness contract, OR (b) we're fanning out to many parallel
tool invocations across worker pools and need replay-on-crash.
Neither is in Phase 0–6 scope.

---

## Context & memory strategy

A long agent run is the easiest place to burn money + lose
correctness. Most of what looks like "we need agent memory" is
actually a context-management problem with a clean answer once you
see where state actually lives.

### State lives in four layers, not one

| Layer | Persistence | Capacity | Cost | What's there |
|---|---|---|---|---|
| **LLM context** | Per call | 200k tokens (Sonnet) | $$$ — re-read every call | Reasoning chain, the *current* tool calls + results |
| **`ipykernel` RAM** | Container lifetime | Host RAM | Free | DataFrames, loaded models, open file handles, imported modules |
| **Project workdir on disk** | Permanent | Disk | Free | Artifacts, intermediate CSVs, plots, downloaded files |
| **DB (`agent_runs` / `agent_run_steps`)** | Permanent | DB | Free | Plan, completed-step audit, token + cost totals |

The architectural gift from picking **per-project ipykernel +
bind-mount workdir**: most "state" lives in layers 2–4. The LLM
context only needs the *current step's* working set. A run that
loads a 50-MB DataFrame, runs ten transformations on it, and saves
a chart spends almost no context tokens on the DataFrame itself —
`df` lives in the kernel; only `df.head()` outputs flow through
context.

### What actually blows the context budget

Three failure modes — each with a targeted fix:

1. **Multi-step reasoning chain accumulates.** A 30-step plan
   where every step's tool calls + results stay verbatim in
   context. → **Plan mode + per-step context bounding** (Phase 4).
2. **Library retrieval results dumped into context.** Search
   returns 50 chunks × 500 tokens = 25k tokens, only 3 are
   actually useful. → Retrieved chunks land as **chunk references**
   the agent can `read_chunk` on demand, not as inline content.
3. **Error retry loops.** Each failed `python_exec` repushes the
   stack trace into context. → **Reflect node** detects "this
   error has appeared 3× already" and forces a strategy change.

### Phase 2 — do nothing yet

Native 200k context + kernel state covers the Phase 2 demo target
("analyze this CSV and plot it"). No plan mode, no compaction, no
per-step bounding. **Don't pre-engineer**; the kernel is already
doing 80% of the work.

If a Phase 2 user hits the context wall, the kernel pattern says
the right answer is "tell the agent to dump intermediate state to
a file, fresh-start the conversation, read the file back" — which
is the kernel-as-memory behaviour we want anyway.

### Phase 4 — Plan-Execute-Reflect with bounded steps

Three pieces, all landing together:

**(A) Plan mode (structured `plan_json`)**

Two-LLM-call pattern:
1. **Planner** reads user request + project context + tool catalog,
   emits structured plan
2. **Executor** walks the plan step-by-step

`agent_runs.plan_json` shape (already in 0.2 schema):

```jsonc
{
  "version": 1,
  "user_request": "Analyze Q3 sales by region and produce a chart",
  "context": {
    "project_id": "...",
    "library_paths_in_scope": ["/sales/q3/"],
    "files_already_in_workdir": ["sales_q3.xlsx"]
  },
  "steps": [
    {"index": 0, "kind": "library_search",       "goal": "...", "expected_output": "list of doc_ids"},
    {"index": 1, "kind": "import_from_library",  "depends_on": [0]},
    {"index": 2, "kind": "python_exec",          "goal": "load + group by region"},
    {"index": 3, "kind": "python_exec",          "goal": "plot + save outputs/q3_by_region.png"},
    {"index": 4, "kind": "summarize",            "goal": "markdown summary"}
  ],
  "termination": {
    "max_steps": 10,
    "max_cost_usd": 2.00,
    "max_wall_seconds": 600
  }
}
```

**HITL gate** between Plan and Execute: opt-in via project setting.
The plan renders in the chat UI as a checklist; user can edit /
approve / cancel before any tool runs. Mirrors Claude Computer
Use's "I'm about to do these steps, OK?" pattern.

**Plan revision**: if a step fails irrecoverably, Reflect kicks
the planner back in with the failure context to produce a revised
tail of the plan (steps after the failure index).

**(B) Per-step context bounding**

Each Executor LLM call gets:
* System prompt (~3k)
* Plan summary (~2k)
* Completed-step one-liner summaries × N (~5k)
* This step's `goal` + `expected_output` (~1k)
* Tool catalog (~3k)
* Tool calls + results within THIS step only (~5–50k)

Average ~30k tokens per step → comfortably under 200k → cheap +
fast + accurate (avoids the empirical lost-in-the-middle accuracy
drop past ~150k).

**(C) Auto-compaction at 75% threshold (within a step)**

Backstop for cases where one step's tool result is huge (a
`python_exec` printing 100k rows of a DataFrame). When the step's
running context hits 75% of the model's window:
1. Trigger summarize-and-drop using the same LLM (or a cheap
   sidecar like Haiku 4)
2. Keep system prompt + plan + last 3 turns verbatim
3. Earlier turns get replaced by a structured summary block

Config: `agent.context_management.compaction_threshold: 0.75` (off
by default until Phase 4 ships).

Pattern is exactly what Claude Code does at ~155k / 78%; we
don't need to invent it.

**(D) Cost ceiling — more important than memory**

A buggy plan running unattended at $0.60/call is a real risk.
Phase 4 enforces:
* Per-project budget cap (operator config: default $5/run, $50/day)
* Plan-time cost preview ("this plan estimates 8 LLM calls × ~30k
  tokens = ~$0.50") shown to the user before Execute
* Hard pause when run approaches cap; user confirms to continue
* `agent_runs.total_cost_usd` enforced at step boundary

### Phase 7+ — cross-session agent memory (deferred, possibly never)

"Agent memory" in the popular sense — Letta / MemGPT / Mem0
"the agent learns user preferences across sessions and surfaces
them automatically" — is **not on our roadmap**. Reasoning:

1. **The other four layers already are memory.** The Library is
   semantic memory (operator-curated knowledge); the project
   workdir is episodic memory (what we did last session); plan
   templates (Phase 7+ if ever) would be procedural memory. All
   transparent, inspectable, editable.
2. **Self-hosted scale doesn't earn it.** ChatGPT's memory layer
   exists because they have hundreds of millions of users
   generating preference signal. Our deployments have 5–50.
3. **It opens a privacy / accountability hole.** "When does the
   agent decide to write a memory? What if it's wrong? Can the
   user inspect / edit / delete every memory?" — these are real
   questions with non-trivial answers, and getting them wrong
   loses customer trust.
4. **Cross-project leakage.** A memory like "Alice prefers tables"
   that bleeds into Alice's work on a different project — or
   worse, into a project where Alice is a viewer of someone
   else's workdir — is exactly the kind of subtle authz bug a
   self-hosted product can't afford.

If a customer eventually asks for it, the integration story is:
plug Letta or Mem0 in as a tool the agent calls (`recall(query)` /
`remember(fact)`), keep the storage in our DB so it's
operator-auditable, gate writes on user confirmation. Not a
Phase 0–6 task.

### The "memory" taxonomy, mapped to OpenCraig

| Memory kind | Where it lives | Who manages it |
|---|---|---|
| **Working memory** (this step) | LLM context | LLM itself |
| **Episodic memory** (this run) | `agent_run_steps` DB + workdir files | Service layer auto-writes |
| **Semantic memory** (facts that matter) | Library | User curates |
| **Procedural memory** (how to do this kind of work) | None today; Phase 7+ might add prompt templates per project type | n/a |
| **Cross-session preference** | None — and probably never | n/a |

---

## Data model

```
auth_users
   │ owner_id
   ↓
projects                 ← NEW
   id (uuid)
   name
   description
   workdir_path          (storage/projects/<id>/)
   owner_id              (sole writer — see "owner-write + read-only
                          share" decision)
   shared_with (jsonb)   ← list[{user_id, role:'r'}] — viewers only,
                          no 'rw'. UI exposed Phase 6+
   created_at
   updated_at

   │ project_id
   ├──────────────────────┬───────────────────────┬───────────────────┐
   ↓                      ↓                       ↓                   ↓
agent_runs           artifacts              execution_sessions   conversations
   id                   id                       id                  (existing —
   project_id           project_id               project_id          adds project_id FK
   conversation_id      run_id (nullable)        container_id          to bind a chat
   user_id              produced_by_step         status                to a project)
   plan (jsonb)         path (relative)          last_active_at
   status               mime
   total_tokens         size_bytes
   total_cost_usd       sha256
   started_at           lineage (jsonb)
   completed_at           {sources: [chunk_ids, urls, prior_artifact_ids]}
   error (nullable)     created_at

   │ run_id
   ↓
agent_run_steps          ← granular log of each plan step
   id
   run_id
   step_index
   role (planner / executor / critic / sub-agent)
   tool_call (jsonb, nullable)
   result (jsonb)
   tokens_in / tokens_out
   wall_ms
   status (pending / running / done / failed / skipped)
   started_at / completed_at
```

**Notable invariants:**

* `conversations.project_id` is **nullable** — chats not tied to a
  Project keep working as today (just Q&A, no agent workspace).
* `Artifact.lineage.sources` contains both `chunk_id` references
  (citations into the Library — survives KB updates because
  chunks are stable IDs) AND `url` strings (web sources, snapshot
  by sha256 of fetched content) AND prior `artifact_id` references
  (when an artifact is built from another artifact).
* `agent_runs.plan` stores the planner's output as structured
  JSON the executor walks; not free-form text.

---

## Filesystem layout

```
storage/
├── blobs/                           # Library content-addressed blobs (existing)
│   └── ab/cd/abcd...sha256.pdf
├── chroma/                          # Library vector index (existing)
├── projects/                        # NEW — agent workspace
│   ├── <project_id>/
│   │   ├── inputs/                  # files imported from Library or uploaded
│   │   ├── outputs/                 # artifacts the user is meant to keep
│   │   ├── scratch/                 # intermediate files (not Artifact rows)
│   │   ├── .agent-state/            # plan checkpoints, retry counters
│   │   └── README.md                # auto-generated description (editable)
│   └── ...
├── user-envs/                       # NEW — per-user persistent runtime volumes
│   └── <user_id>/
│       ├── r/                       # IRkernel + R + tidyverse (lazy-installed)
│       ├── julia/                   # IJulia + base packages
│       ├── node/                    # nvm + node + npm globals
│       └── ...                       (any runtime agent installs on demand)
└── kernels/                         # NEW — per-execution-session state
    └── <session_id>/                  (mounts back into project workdir)
```

`user-envs/<user_id>/` is bind-mounted into that user's container at
`/workspace/.envs/`. It outlives both individual project workdirs
and container reaping — the user's R install is still there next
session, next project.

The `inputs / outputs / scratch` separation is a **soft convention**
the agent is told to follow in its system prompt; not enforced at
the API level. UI shows them as separate sections so the user
intuitively sees `outputs/` as "what to keep" and `scratch/` as
"safe to delete".

---

## Tools the agent gets (full inventory after all phases)

Decision (May 2026, after evaluating "minimum primitives vs.
specialised tools" against OpenClaw / Claude Code / AutoGen): we
keep the tool surface **small** — `python_exec` + `bash_exec` plus
specific-purpose tools where shell isn't the right primitive
(library bridge, runtime install, web search, skills). File ops
(`list_files` / `read_file` / `write_file`) were originally
scheduled for Phase 3 and deliberately **dropped** — fully
redundant with `python_exec("os.listdir(...)")` /
`bash_exec("ls -la")` / `python_exec("Path('...').write_text(...)")`.
Artifact tracking (the only reason `write_file` would have earned
its keep) instead happens via **auto-scan of `outputs/`** after
every `python_exec` / `bash_exec` call (Phase 2.7).

| Tool | Phase | What it does |
|---|---|---|
| `search_library` | already exists | Retrieves chunks from the Library (existing RAG) |
| `read_chunk` | already exists | Pulls a specific chunk by ID |
| `read_tree` | already exists | Document outline navigation |
| **`python_exec`** | 2 | Run Python in the project's per-project ipykernel. **Persistent state** across calls — `df = pd.read_excel(...)` once, then `df.head()` / `df.groupby(...)` / `df.plot()` in subsequent calls hit the same in-memory df. The workhorse for any analytical / multi-step work |
| **`bash_exec`** | 2 | Run shell in the same container. **No persistent state** — each call is fresh. Use for shelling out to the 25 pre-installed CLIs (`pdftotext` / `xsv` / `jq` / `pandoc` / `ffmpeg` / `tesseract` / `rg` / `find` / etc.) where Python's `subprocess.run([...])` boilerplate is wasteful |
| **`import_from_library`** | 2 | Copy a Library doc the user has read access to into the project workdir's `inputs/` (records an Artifact with `lineage.source = chunk/doc reference`). Same backend service as the Phase 1 manual UI button |
| **`install_runtime`** | 2 | Install a non-Python language runtime (R / Julia / Node / Rust / …) into the user's persistent `/workspace/.envs/` volume on first need; registers `<lang>_exec` for the rest of the run |
| **`promote_to_library`** | 3 | Push an artifact back into the Library (becomes indexed) |
| **`run_skill`** | 5+ | Load a markdown-defined reusable workflow (`<workspace>/skills/<name>/SKILL.md`) and run it with parameters. The team's "Q3 review" / "data cleanup" / "weekly report" patterns get codified as skills, shared across projects + users (see Skills section below) |
| `web_search` | 5 | External web search via Tavily/Brave/SearXNG |
| `fetch_url` | 5 | Download + parse a URL into markdown |

---

## Phased plan (high-level — see Phase 0 / Phase 1 detail below)

| Phase | What ships | Demo capability |
|---|---|---|
| **0** | Rename Workspace → Library; new empty Workspace surface; data model; placeholder Project CRUD (single-user) | "We have two surfaces now" — just architecture |
| **1** | Workspace UI = file manager over project workdir; manual "import from Library" UI; Chat ↔ Project binding | User can create a Project, pull a Library doc into it, open a chat against it |
| **2** | `python_exec` + `bash_exec` + `install_runtime` + **`import_from_library`** via per-user Docker container + `jupyter_client`; rich-output rendering (figures saved to `scratch/_rich_outputs/`); auto-Artifact scan of `outputs/` after every code run; sandbox image with ~25 CLI tools; per-user `.envs/` for lazy R/Julia/Node | "Find Q3 sales and analyze it" works end-to-end (Library search → import → python_exec → chart) |
| **3** | `promote_to_library` (push agent-produced artifacts back into Library, indexed); auto-Artifact scan extended with `outputs/` ↔ Library bidirectional mapping | Agent's deliverables become first-class Library content |
| **4** | Plan-Execute-Reflect orchestrator with structured `plan_json`; per-step context bounding; auto-compaction at 75% threshold; cost ceiling + plan-time cost preview; HITL gate (opt-in) | "Compare these 5 contracts and produce a tracker xlsx" works without context blow-up; runs over budget pause for confirmation |
| **5** | Web search (default-on) + fetch_url + injection defense; **Skills** (DB-backed reusable workflows, see below) + `search_skills` / `run_skill` agent primitives | Team codifies "Q3 review" once; agent finds it via NL search next quarter and re-runs with one call. Adding skills costs zero tool-catalog tokens |
| **6** | Artifact lineage UI; project export; cost dashboard; **Skills marketplace UI** (browse / fork / share within workspace); scheduled-task triggers (cron) | Sale-ready polish — team workflow ergonomics |

Phase 0 + Phase 1 are designed to be **3 weeks total**. The rest
is 12-18 additional weeks depending on team size.

---

## Skills — reusable agent workflows for teams

**Why this exists:** the team-knowledge-management positioning
demands that work patterns are **reusable across people and time**.
Alice writes the steps to do "Q3 sales review" once; bob re-runs
the same flow next quarter via one tool call. Without skills, every
team member re-prompts from scratch every cycle — losing
institutional knowledge that lives in chat history nobody re-reads.

### The architectural insight: skills are NOT tools

Every ToolSpec costs **100–300 tokens** in the OpenAI/Anthropic
tool format and ships in **every** LLM call's tool catalog. With
30 skills as separate tools, we'd burn ~5K tokens per call just on
tool descriptions — Phase 4's plan-mode does 10–20 calls per task,
that's 100K tokens of pure overhead.

**Skills must NOT be exposed as individual tools.** Instead, the
agent gets two new primitives that mediate access:

```
search_skills(query) → top-K matching skill summaries
run_skill(skill_id, params) → loads body + executes via Phase 4 orchestrator
```

The skill **body** only enters LLM context once — when `run_skill`
loads it as a plan. It's never in the tool catalog.

This mirrors how Anthropic's Claude Skills works in production: a
skill registry indexed for retrieval, not a tool-per-skill catalog.

### DB-first storage (not filesystem)

Earlier drafts of this doc proposed
``<workspace>/skills/<name>/SKILL.md`` as the canonical form.
**That was wrong** for a team-collaboration product:

| Concern | Filesystem | DB |
|---|---|---|
| Cross-user / cross-project search | walk + grep | indexed query |
| `search_skills` semantic match (NL query → skill) | rebuild index per call | reuse existing vector store |
| Version history + rollback | git, if operator opted in | first-class table |
| Permission inheritance (workspace + project scope) | filesystem ACLs are messy | reuse `Folder.shared_with` pattern |
| Audit (who changed what) | git log if it's tracked | `audit_log` table already there |
| Concurrent edits | flock / lose | DB transaction |

**DB is canonical**. Filesystem export is an opt-in operator
feature for git backup (``opencraig skills export → ./skills/``)
— not the source of truth.

### Schema

Phase 5 alembic migration. **Two tables, no embedding table** —
embeddings live in the existing vector store (see "Embeddings via
the existing VectorStore" below):

```python
class Skill(Base):
    __tablename__ = "skills"
    skill_id: str                          # 32-char hex, primary key
    name: str                              # unique within (scope, project_id)
    scope: 'workspace' | 'project'         # workspace = team-shared; project = local to one project
    project_id: str | None                 # FK projects.project_id, only for scope='project'
    description: str                       # what `search_skills` shows the LLM (NL, ~1-3 sentences)
    body: str                              # full markdown the agent walks on run_skill
    params_schema: dict                    # JSON-schema for run_skill params

    version: str                           # semver — bumps create a SkillVersion row
    created_by_user_id: str FK auth_users  # original author (immutable)
    last_modified_by_user_id: str FK auth_users
    import_source: dict | None             # null = locally created; else
                                           # {type:'marketplace', url, original_skill_id, version, imported_at}
    metadata_json: dict                    # tags, category, deprecated flag, etc.
    trashed_metadata: dict | None          # soft-delete marker, mirrors Project.trashed_metadata
    created_at, updated_at

class SkillVersion(Base):                  # rollback / diff history
    skill_version_id: str
    skill_id: str FK
    version: str
    body: str                              # snapshot at this version
    params_schema: dict
    description: str
    changed_by_user_id: str FK auth_users
    changed_at: datetime
    change_note: str | None
```

**No `shared_with` on Skill.** Visibility is **derived** from the
combination of `scope` + the parent container's authz:

| `scope` | Who can `search_skills` + `run_skill`? | Who can edit / delete? |
|---|---|---|
| `workspace` | Any authenticated user in the deployment | admin role OR `created_by_user_id` |
| `project` | Anyone with project read (owner + viewers) | project owner OR admin OR `created_by_user_id` |

This avoids duplicating ACL bookkeeping that already exists on
``Folder.shared_with`` (workspace docs) and ``Project.shared_with``
(per-project access). Skills inherit; they don't track their own.

**Audit trail** lives in `created_by_user_id` (immutable, original
attribution), `last_modified_by_user_id` (most-recent editor), and
the `SkillVersion` rows (full history of who changed what when).
Same `audit_log` row writes as folder/project mutations.

### Embeddings via the existing VectorStore (NOT a separate table)

Earlier draft proposed a `skill_embeddings` table — wrong. We
already have a vector backend (`cfg.persistence.vector` configured
to chromadb / pgvector / qdrant / etc.). Skills embed there too;
two tables of vectors in one deployment would mean double the
operational surface for no benefit.

Implementation:

* On skill create / update: backend computes
  `embed(description + "\n" + body)` via the existing
  ``state.embedder``, upserts into the vector store under a
  **separate namespace / collection** (chromadb collection name
  ``opencraig_skills``; pgvector with a ``kind='skill'``
  discriminator; qdrant collection; etc.) keyed on ``skill_id``,
  with metadata `{kind:'skill', scope, project_id, owner_user_id}`.
* On delete / trash: remove the vector entry. (Soft-delete keeps
  the row but pulls the vector — matched skills wouldn't be
  runnable; cleaner to drop them from search.)

**Phase 5 small refactor on `VectorStore`**: today the API is
implicitly chunk-flavoured (``vec.search(...)`` searches the
chunks namespace). Phase 5 adds a tiny abstraction:

```python
class VectorStore:
    # Existing — sugar for namespace='chunks'
    def search(self, vec, *, top_k=10, filter=None) -> list[VectorHit]: ...

    # NEW — collection-aware
    def search_namespace(
        self, namespace: str, vec, *, top_k=10, filter=None
    ) -> list[VectorHit]: ...
    def upsert_namespace(
        self, namespace: str, items: list[VectorItem]
    ) -> None: ...
    def delete_namespace(
        self, namespace: str, ids: list[str]
    ) -> None: ...
```

Each backend's implementation maps `namespace` to its native
notion (chroma collection / qdrant collection / pgvector
``kind`` filter / ...). Existing chunk code keeps working
unchanged via the sugared `.search()`.

### CRUD interfaces (HTTP + Service + future marketplace)

Same service powers UI editing + agent-driven creation + future
marketplace imports. They differ only in the metadata stamped on
the row.

**HTTP API** (`api/routes/skills.py`):

```
POST   /api/v1/skills                          create workspace skill (admin or self-curate)
POST   /api/v1/projects/{id}/skills            create project skill (project owner)
PATCH  /api/v1/skills/{skill_id}               edit (creator / admin / project owner)
DELETE /api/v1/skills/{skill_id}               soft-delete (same gate as PATCH)
GET    /api/v1/skills?scope=&project_id=       list (authz-filtered)
GET    /api/v1/skills/{skill_id}               detail
GET    /api/v1/skills/{skill_id}/versions      version history
POST   /api/v1/skills/{skill_id}/restore       roll back to a SkillVersion
POST   /api/v1/skills/import                   import from a marketplace URL
                                                 → calls SkillService.import_from_url(url)
                                                 → stamps import_source metadata
                                                 → otherwise behaves like a fresh create
```

**Service**: `persistence/skill_service.py`

```python
class SkillService:
    def create(*, name, scope, project_id, description, body,
               params_schema, by_user_id, import_source=None) -> Skill: ...
    def update(*, skill_id, by_user_id, **fields) -> Skill: ...
    def delete(*, skill_id, by_user_id) -> Skill: ...
    def restore_version(*, skill_id, target_version, by_user_id) -> Skill: ...
    def list(*, principal, scope=None, project_id=None) -> list[Skill]: ...
    def search(*, principal, query, top_k=5, scope=None) -> list[SearchHit]: ...
    def import_from_url(*, url, by_user_id) -> Skill: ...
```

Every mutation:
* writes / updates the SQL row,
* re-embeds + upserts into the vector store,
* writes a `SkillVersion` snapshot,
* writes an `audit_log` row (`skill.create` / `skill.update` /
  `skill.delete` / `skill.import` / `skill.restore`).

**Optional Phase 5 polish — `create_skill` agent tool.** Lets
the agent codify an ad-hoc workflow it just performed into a
reusable skill: "Hey agent, save the steps you just did as a skill
called `q3-sales-review`." Adds ~200 tokens to the catalog;
behind a config flag (`agent.allow_skill_authoring: bool`,
default off — operator opts in for power-user deployments). Calls
the same `SkillService.create` underneath.

### Two new agent primitives

**`search_skills(query, scope?, top_k=5)`** — NL query against
indexed skills. Returns summaries, NOT bodies:

```jsonc
[
  {
    "skill_id": "sk_abc123",
    "name": "q3-sales-review",
    "description": "Generate quarterly sales tracker from Library xlsx files",
    "params_summary": ["quarter:Q1|Q2|Q3|Q4", "year:int"],
    "scope": "workspace",
    "version": "1.2.0"
  },
  ...
]
```

LLM picks one, calls `run_skill`.

**`run_skill(skill_id, params)`** — loads body, validates params,
substitutes ``{{var}}`` placeholders, injects the result as a
**structured plan** into Phase 4's ``agent_runs.plan_json``, kicks
off the orchestrator. Returns the run_id immediately:

```jsonc
{ "run_id": "run_xyz", "status": "running", "plan_steps": 5 }
```

Skill body is now the plan; Plan-Execute-Reflect walks it with the
same budget caps + HITL gates as ad-hoc plans. Skills compose
cleanly with Phase 4 — they ARE pre-canned plans.

### SKILL body format (the markdown the orchestrator walks)

Stored in the DB ``body`` column. Frontmatter is parsed into
``params_schema`` etc. at save time; the body that hits LLM context
on `run_skill` is just the prose:

```markdown
You are running the {{quarter}} {{year}} sales review.

Steps:
1. Search the Library at `/sales/` for filenames matching
   "{{quarter}}" or "{{quarter}}_{{year}}".
2. import_from_library each match into the project's `inputs/`.
3. python_exec: load every xlsx with `pd.ExcelFile` to inspect
   sheets (headers often on row 2–4 with merged cells); concatenate;
   drop subtotal rows; produce a regional + product-line breakdown.
4. python_exec: render as a bar chart + markdown summary into
   `outputs/`.
5. promote_to_library on `outputs/{{quarter}}_review.md` so next
   quarter's run finds it.
```

### Authz — reuses the folder-grant model

* **Workspace skills**: anyone with read on the corresponding
  workspace folder can `search_skills` + `run_skill`. Edit/delete
  needs admin or skill owner.
* **Project skills**: project owner edits/deletes; project members
  (incl. read-only viewers) can search/run — running doesn't write
  into the project workdir directly; the orchestrator's tool calls
  go through the same authz checks they would standalone.
* **Cross-workspace publishing**: a Phase 6 marketplace UI lets
  admins promote a skill to a public registry within the deployment
  (or globally, if a future "ClawHub-style" remote registry lands —
  we'd model it as a separate scope).

### The agent's tool budget after Skills

Phase 5+ tool inventory the LLM sees on every call:

| # | Tool | Why it's a primitive |
|---|---|---|
| 1 | `python_exec` | Code with persistent kernel state — irreducible |
| 2 | `bash_exec` | Shell + 25 CLIs — irreducible |
| 3 | `search_library` (`search_vector`) | Library retrieval — irreducible |
| 4 | `read_chunk` | Specific passage by id |
| 5 | `read_tree` | Document outline |
| 6 | `graph_explore` | KG traversal |
| 7 | `web_search` | External info |
| 8 | `import_from_library` | Library → workdir auth-gated bridge |
| 9 | `install_runtime` | Lazy R/Julia/Node install |
| 10 | `promote_to_library` | Workdir → Library |
| 11 | `rerank` | Post-retrieval refinement |
| 12 | **`search_skills`** | Find a reusable workflow |
| 13 | **`run_skill`** | Execute one |

13 primitives total. Skills add only 2 (~400–600 tokens) but unlock
**unbounded** team-defined workflows. Adding a new skill costs zero
tool-catalog tokens — only the body when it's actually run.

### What we did NOT borrow

* **OpenClaw `nodes`** (companion device interfaces with voice +
  canvas) — not in our product surface
* **Live Canvas / A2UI rendering** — different UX from chat trace;
  Phase 6+ candidate if a customer asks (browse history's
  "agent-driven dashboard" use cases first)
* **Multi-channel routing** (Slack / WhatsApp / Discord) — Phase 6
  integration if asked; no architecture change needed (push
  notifications hang off `agent_runs` completion event)
* **OpenClaw's "tools run on host by default"** — incompatible with
  our multi-user threat model; we always sandbox
* **OpenClaw's filesystem-only skills** — incompatible with our
  team-collaboration positioning; DB-first instead

---

## Decisions

### Settled (this round)

| Decision | Outcome |
|---|---|
| **Sandbox isolation boundary** | **Per-user Docker container + per-project ipykernel**, not per-project container, not shared pool, not bare subprocess |
| **Kernel orchestration** | **`jupyter_client` + `docker` SDK** managed by our own `SandboxManager`, NOT JupyterHub. AutoGen's `DockerJupyterCodeExecutor` is the reference implementation we adapt from |
| **Filesystem model** | **Bind-mount** `storage/projects/<id>/` from host to container `/workdir/<id>/`. Single source of truth on host fs; Workspace UI reads host fs directly (no docker exec round-trip) |
| **Container privilege model** | **`userns-remap` enabled** at the docker daemon level. Container "root" maps to host uid 100000+. Agent can `apt install` / `mamba install` freely; bind-mount files on host are owned by the unprivileged remapped uid (no host-root pollution); container escape lands in unprivileged-user space, not host root |
| **Multi-language** | Python + Bash kernels day 1; ~25 CLI tools pre-installed in `:py3.13` sandbox image. **Other languages (R, Julia, Node, Rust, …) are an agent capability**, not image variants: agent calls `install_runtime("r")` on first need, which installs into the user's persistent `/workspace/.envs/r/` volume and registers an `r_exec` tool for the rest of the run. Cold install ~1–3 min once per user; every subsequent project for that user is zero-wait. Operators can pre-warm via `agent.runtime_preinstall` |
| **Observability** | OpenLLMetry → existing OTel collector + (optional) Langfuse self-host for LLM-specific dashboard. Sandbox monitor + per-project run history are OpenCraig's own UI |
| **Workflow engine** | None (Postgres `agent_runs` row); revisit Temporal/Restate if durable-wait > 1 min lands as a real customer ask |
| **Web-search timing** | Phase 5, not earlier |

### Settled (operator decisions, May 2026)

| Decision | Outcome |
|---|---|
| **Project = owner-write + read-only share** | A project has one writer (the owner) and zero or more **read-only viewers**. Owner runs agents, edits files, deletes; viewers can browse the workdir + run history without touching anything. No `rw` role exists. Rationale: a project owns the agent's live kernel state + run history + intermediate artifacts, and multi-writer on those would create races + state pollution. But **read-only share is genuinely useful** — consultants showing progress to clients, leads watching team members' agent runs, audit reviewers. Library keeps its existing folder-grant multi-user model (read + write share). UI exposure of project read-only-share is **deferred to Phase 6+** so we don't carry the dialog through Phase 0-5 reviews — the schema, service, routes, and component file all exist today (`shared_with` column, `add_or_update_member` rejecting `rw`, `ProjectMembersDialog.vue` not mounted) so the eventual UI rollout is half a day, not half a week. |
| **Demo use case** | None — operator drives validation against their own real workflows; we don't ship demo project templates |
| **Web search default** | **On** at Phase 5 ship-time; operator can disable per-deployment via `agent.web_search.enabled: false`. (Earlier we'd planned default-off for the citation-purity argument; operator's call is that the value of fresher external context outweighs the audit-trail risk, and the lineage panel already distinguishes "sourced from URL X at sha Y" from "sourced from Library chunk Z" so users can see what came from where.) |
| **Built-in templates** | **None.** No `examples/templates/` directory, no "starter projects" UX. Phase 6 dropped that task entirely. |
| **R / Julia in base image** | **No.** Stay with the on-demand `install_runtime` capability; no `agent.runtime_preinstall` config either. Customer base isn't biotech-skewed enough to justify pre-warming. |

### Library → Workspace bridge: PRIORITY UPGRADE

**Decision (May 2026)**: the bridge between the indexed Library and
the per-project workdir is the **headline feature**, not a Phase 3
nice-to-have. The agent UX the operator described:

> "Analyze the Q3 sales situation"
>
> → agent searches Library (with the user's `path_filters`)
> → finds `q3_sales.xlsx` (a doc the user has read access to)
> → pulls it into the current project's `inputs/` workdir
> → runs `python_exec` against the local file
> → drops a chart into `outputs/`

This requires `import_from_library` to ship **with** `python_exec`,
not after it. Moved from **Phase 3 → Phase 2**. Phase 1 also gains
a manual UI affordance ("import from Library" button in the project
workdir view) for the same flow without going through chat.

**Authz boundary (no change to existing system)**: Library
retrieval has always run through the user's `path_filters`; the
agent calling `search_library` / `read_chunk` / `import_from_library`
inherits that filter from the request principal. A user cannot
import a file they wouldn't see in the Library UI — same gate,
new caller.

---

## Non-goals (explicit cuts)

* **Real-time collaboration** — Google-Docs-style multiple users
  editing the same artifact simultaneously. Not Phase 1-6. Single-
  writer with locking is fine.
* **Workflow automation / triggers** — "when this happens, run
  this agent." Phase 6+ schedule cron is the baseline; full
  trigger DAGs (Zapier-style) is out of scope.
* **Custom tool authoring** — users defining their own Python
  tools the agent can call. Possibly Phase 7+; deeply out of
  scope right now.
* **Distributed sandbox cluster** — multiple hosts running kernels.
  Single-host is fine for the target deployment size; scale-out is
  a post-Series-A problem.
* **GUI tool execution** — agent driving a web browser visually
  (Manus does this). Way out of scope; explicitly different from
  the "headless code + file" workflow we're targeting.

---

# Phase 0 — Library / Workspace split + data model

**Goal:** Get the architectural rename + new tables in place so
all subsequent work has a place to land. **Visible result:** a
sidebar with two entries; clicking the new "Workspace" shows an
empty project list with a "Create project" button. Nothing else
works yet, but the shape is right.

**Estimate:** 1 calendar week.

## Tasks

### 0.1 — UI rename: Workspace → Library  (~0.5 day)

* `web/src/router.js`: route `/workspace` renamed to `/library`,
  redirect `/workspace` → `/library` (transitional; will reverse
  when Phase 0.4 lands).
* `web/src/components/AppSidebar.vue`: nav link "Workspace" →
  "Library" with new icon (lucide `BookOpen` instead of `Folder`).
* `web/src/views/Workspace.vue` → `web/src/views/Library.vue`
  (rename file too — the i18n strings already say things like
  "Empty workspace.", which become "Empty library." now).
* i18n updates `web/src/locales/{en,zh}.json`:
  - `nav.workspace` → `nav.library`; values "Workspace" / "工作区"
    → "Library" / "资料库"
  - All strings under `workspace.*` namespace renamed to
    `library.*`
* Find all consumer references: `useWorkspace` composable —
  rename to `useLibrary`. Anywhere the prior name is hardcoded
  (the `dragInProgress` flag's source-app fallback was already
  retired in 7a823e7, but worth a grep).

### 0.2 — Data model + migrations  (~1 day)

* Alembic migration `add_projects_artifacts_runs.py`:
  - `projects` table (per data model above)
  - `agent_runs` table
  - `agent_run_steps` table
  - `artifacts` table
  - `execution_sessions` table
  - Add `project_id` (nullable FK) to `conversations`
* `persistence/models.py`: add ORM rows for each of the above.
* `persistence/project_service.py` (NEW): create / get / list /
  update / delete / share-grant — same shape as
  `persistence/folder_service.py`.

### 0.3 — Filesystem layout  (~0.5 day)

* On project create: `mkdir -p storage/projects/<id>/{inputs,outputs,scratch,.agent-state}`
* Generate `README.md` with project name + description.
* On project delete: soft-delete to `storage/projects/__trash__/`
  (mirroring the Library's recycle bin pattern); hard-purge after
  30 days via the existing `nightly_maintenance.py`.

### 0.4 — Backend skeleton  (~1 day)

* `api/routes/projects.py`:
  - `GET    /api/v1/projects`                       — list the caller's projects (owned + viewer)
  - `POST   /api/v1/projects`                       — create
  - `GET    /api/v1/projects/{id}`                  — detail
  - `PATCH  /api/v1/projects/{id}`                  — rename / edit description (owner only)
  - `DELETE /api/v1/projects/{id}`                  — soft-delete (owner only)
  - `GET    /api/v1/projects/{id}/members`          — list viewers
  - `POST   /api/v1/projects/{id}/members`          — invite viewer (role='r' only)
  - `DELETE /api/v1/projects/{id}/members/{uid}`    — remove viewer
* Authz: `read` works for owner / admin / any shared_with member;
  `write` / `share` / `delete` are owner-or-admin only — viewers
  can browse but never mutate. The route layer rejects
  `role='rw'` at the pydantic boundary so no codepath can mint a
  write-share grant. 404 on no-access (existence privacy).
* No `PATCH /members/{uid}` route — there's no role to change
  (only 'r' exists). When Phase 6+ adds richer roles or owner
  transfer, the patch route lands then.
* Audit log: `project.create / project.rename / project.delete /
  project.update / project.share / project.unshare`.

### 0.5 — Frontend Workspace skeleton  (~1 day)

* `web/src/views/Workspace.vue` (NEW — different from the renamed
  one): empty-state shows "No projects yet" + a "Create project"
  button.
* New API client `web/src/api/projects.js` — list / create / etc.
* Sidebar link "Workspace" → `/workspace` (the new route).
* Project list page: simple grid of cards `(name, description,
  last_active)`; click → project detail page.
* Project detail page: just shows name, description, "no chats
  yet" placeholder.
* `ProjectMembersDialog.vue` exists on disk but is **NOT mounted**
  anywhere in Phase 0–5. It's a read-only-viewer manager (email
  invite + remove, no role select) reserved for the Phase 6+ UI
  rollout — see the "Project = owner-write + read-only share"
  decision. The backend routes + service exist now so the
  eventual UI mount is half a day's work.

### 0.6 — Tests + smoke  (~0.5 day)

* `tests/test_route_projects.py`: CRUD + ownership boundary +
  read-only-share roundtrip (alice invites bob with role='r',
  bob sees the project on his list with role 'r', bob can GET
  detail but PATCH/DELETE/POST-members all 404, role='rw' is
  rejected at the pydantic boundary).
* `tests/test_no_phone_home.py`: no new SDKs.
* Manual smoke: register two users, create a project as A, verify
  B cannot list / open / mutate it. (The viewer-share UX itself
  is a Phase 6+ smoke since the dialog isn't mounted yet.)

## Phase 0 acceptance checklist

- [ ] Sidebar shows two distinct entries: "Library" + "Workspace"
- [ ] `/library` renders the existing folder-tree UI (same
      functionality, just rebranded)
- [ ] `/workspace` renders an empty project list
- [ ] Create-project button works; project appears in the list
- [ ] Project's workdir actually exists on disk under
      `storage/projects/<id>/`
- [ ] Another user cannot list / open / mutate a project they
      don't own (404)
- [ ] Viewer-share path works end-to-end: owner can `POST /members`
      with role='r', viewer sees the project with role='r', viewer's
      writes 404 (UI for this is Phase 6+; route + service must work
      today)
- [ ] All existing tests still pass; build succeeds
- [ ] No Library-related functionality is broken by the rename

---

# Phase 1 — Workspace as a project file manager + Library import + chat binding

**Goal:** Phase 0 has the architecture; Phase 1 makes the
Workspace **useful** as a project-scoped file manager **with a
manual Library → Workspace import flow** (no agent capability
yet). The user can:
* Upload files directly into a project workdir (drag-drop / picker)
* **Pick a doc from the Library and copy it into the project's `inputs/`** — the operator-visible UI version of `import_from_library` that ships in Phase 2 as an agent tool
* Open a chat scoped to a project — retrieval still hits the
  Library, but the chat carries `project_id` so subsequent phases'
  agent work has somewhere to land

**Estimate:** 2 calendar weeks.

## Tasks

### 1.1 — File operations on the project workdir  (~3 days)

Backend:

* `api/routes/projects.py` extended:
  - `GET    /api/v1/projects/{id}/files?path=...` — list files in
    a workdir subdir
  - `POST   /api/v1/projects/{id}/files`           — upload a file
    (multipart) into a relative path
  - `GET    /api/v1/projects/{id}/files/download?path=...` — download
  - `PATCH  /api/v1/projects/{id}/files/move`      — rename / move
  - `DELETE /api/v1/projects/{id}/files`           — delete (soft;
    moves to `.trash/` inside the project)
  - `POST   /api/v1/projects/{id}/files/mkdir`     — create directory
* Path safety: every API takes a relative path and rejects `..`,
  absolute paths, symlinks pointing outside the workdir.
* Project-level quota: configurable max bytes per project (default
  10 GB), tracked on every write.

Frontend:

* `web/src/views/ProjectDetail.vue` (or extend `Workspace.vue`):
  three-pane layout — file tree on left, file preview / list on
  right, project metadata / chat starter on top.
* File tree component — start with a fork of Library's grid/list
  view (a future refactor merges them; now just copy).
* Drag-drop upload (reuse `GlobalUploadPanel.vue` patterns).
* Inline preview for: text files, markdown, csv (table view),
  json (formatted), images. PDF and xlsx defer to a "click to
  download" link in Phase 1; rich preview is Phase 6 polish.

### 1.2 — Chat ↔ Project binding  (~2 days)

* URL convention: `/chat?project=<id>` — when present, the chat
  is bound to the project.
* On send, if the conversation is bound to a project, write
  `Conversation.project_id = <id>` (existing column added in 0.2).
* Chat empty state shows project context: "Working on: <project
  name>" + a "switch project" link.
* New conversation in a project context inherits the project
  binding.
* Chat list (sidebar) groups conversations by project when bound;
  unbound conversations stay in the global list.
* Project detail page lists all conversations for the project,
  click → opens that chat.

### 1.3 — System prompt awareness  (~0.5 day)

* When a conversation has a `project_id`, the agent's system
  prompt is augmented with:
  - The project's name + description
  - A list of files currently in the workdir (paths only, not
    contents — too expensive)
  - A short reminder: "You can already retrieve from the Library;
    additional tools (file I/O, code execution) ship in later
    phases — don't claim you can use them yet"
* Importantly: the agent **cannot yet** read project files. The
  retrieval surface is still the Library only. This avoids
  scope creep while we ship the file-manager piece cleanly.

### 1.4 — Library → Workspace import (manual UI)  (~2 days)

This is the operator-visible counterpart to the `import_from_library`
agent tool that ships in Phase 2. Phase 1 lets a user do it by hand
so the storage layout + Artifact lineage are exercised end-to-end
before the agent ever calls it.

Backend:

* `POST /api/v1/projects/{id}/import` body
  `{doc_id, target_subdir?: "inputs"}` — copies the source blob
  from the Library's content-addressed store into the project
  workdir, creates an `Artifact` row with
  `lineage_json = {sources: [{type: "doc", doc_id}]}`.
* Authz: caller must (a) be the project owner AND (b) have read
  access to the Library doc. Doc-access check reuses
  `require_doc_access` from `api/deps.py` (pre-existing helper),
  so the rule is identical to the Library UI's "can I open this
  doc" gate. There's no path for an importer to bypass — owner
  is the only person who can write into a project workdir, and
  the doc-access check is the same one already enforced
  everywhere else.
* Idempotent: importing the same doc_id twice into the same
  project re-points to the existing artifact rather than
  duplicating the file.

Frontend:

* "Import from Library" button on the project detail page →
  opens a `LibraryDocPicker.vue` modal: tree view of folders the
  user has read on, search box, multi-select.
* Imported docs land in `inputs/` by default with a small
  "imported from /path/in/library" subscript visible in the
  workdir file list.

### 1.5 — Audit log integration  (~0.5 day)

* All Phase 1 file operations write to `audit_log`:
  `project.file.upload / .download / .move / .delete / .mkdir`.
* `project_id` recorded as the audit `target_id` so the existing
  Activity admin page filters cleanly by project.

### 1.6 — Tests + smoke  (~1 day)

* `tests/test_project_files.py`: upload / list / download / move /
  delete, including `..` path-traversal rejection.
* `tests/test_project_quota.py`: oversize upload rejected, partial
  uploads cleaned up.
* End-to-end smoke: alice creates project, uploads a CSV directly,
  also uses "Import from Library" to pull in `q3_sales.xlsx` (a doc
  she has read on in the Library), opens a chat scoped to the
  project, conversation row carries `project_id`. Bob (a different
  user with NO grant on alice's Library folder) cannot import that
  same doc into HIS own project — the doc-access check refuses.

### 1.7 — Docs  (~0.5 day)

* `docs/architecture.md`: add section "Workspace + Projects"
  explaining the Library/Workspace split + project model.
* `docs/operations/workspace.md`: storage layout, quota config,
  per-project backup notes (referencing scripts/backup.sh
  changes — projects volume is included automatically).
* README updated: "What you get" table gets a new row for
  Workspace; Phase ladder in roadmap section reflects what's
  actually shipped.

## Phase 1 acceptance checklist

- [ ] User can create a project, upload files, organize them in
      sub-folders, rename, move, delete (with restore from
      project-local trash)
- [ ] "Import from Library" button copies a doc into the project
      workdir's `inputs/`, recording an Artifact with lineage
      pointing back to the original doc
- [ ] User who lacks read access to a Library doc cannot import it
      (404 — same gate as Library UI)
- [ ] Chat opened from a project is bound to it; conversation row
      has `project_id` set
- [ ] All Library functionality (search, KG, ingestion, member
      sharing) works exactly as before — Workspace doesn't bleed
      into it
- [ ] Quota enforced; oversize upload returns 413 with clear
      error
- [ ] Path traversal attacks (`/api/v1/projects/X/files?path=../../etc/passwd`)
      return 400, not the file
- [ ] Project file operations show up in `/settings/audit`
- [ ] Build green; tests pass; bundle size growth < 50 KB
      gzipped (this phase is mostly backend + UI reuse)

---

## What Phase 0 + Phase 1 do NOT ship

These are deliberate cuts to keep the first 3 weeks bounded:

* **No agent code execution.** `python_exec` and the Docker
  sandbox are Phase 2. Users in Phase 1 can put files in a
  project but the agent can't touch them yet.
* **No agent-driven Library bridge.** Phase 1 ships the *manual*
  "Import from Library" UI; the agent's `import_from_library`
  tool that does the same thing automatically lands in Phase 2
  alongside `python_exec`.
* **No artifact concept for agent outputs.** The `artifacts` table
  exists from 0.2 and Phase 1's manual Library import writes rows
  into it (lineage = doc reference). Agent-produced artifacts wait
  for Phase 2's `python_exec` and Phase 3's `write_file`.
* **No agent runs.** `agent_runs` table exists; remains empty
  through Phase 0-3.
* **No project-sharing UI.** Read-only viewer share works at the
  API level today (used by tests; documented under
  `POST /api/v1/projects/{id}/members`), but the frontend does
  NOT expose a button for it through Phase 5. Users collaborate
  via shared **Library content** + each running their own agent
  in their own project. The viewer-share UI mounts in Phase 6+
  when there's a real customer ask for "show me what your agent
  is doing in your project."

If during demo someone asks "can the agent do X with this file
yet?", the honest answer for Phase 1 is "not yet — the workspace
is here so the agent has a place to write. The agent itself ships
in the next two months."

---

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Sandbox escape (Phase 2) | Low if Docker right; high if subprocess | Critical | Phase 2 must include external security review before merge |
| Long-running tasks lose state on worker crash | Medium | High | Phase 4 must persist `agent_runs` state machine to DB; resume on boot |
| Cost runaway from agentic loops | High | High | Phase 0 already adds `total_cost_usd` column; Phase 4 enforces per-project budget cap + plan-time cost preview + hard-pause at threshold + user-confirm to continue. Treated as more important than memory features. |
| Long-context blow-up on multi-step plans | Medium | High | Phase 4 ships per-step context bounding (planner emits `plan_json`, executor only sees current step's working set + summarized history) + auto-compaction at 75% threshold. Phase 2 relies on native 200k + ipykernel state offloading; if that's not enough we accelerate Phase 4. |
| Naming churn confuses existing users | Medium | Low | Phase 0 ships the redirect `/workspace → /library`; keep it for 6 months |
| Docker isn't available in some operator environments | Medium | Medium | Phase 2 ships with Docker-only support; document `code_execution.enabled: false` config flag for operators who need to disable |
| Library doc imported into a project survives that doc being deleted from Library | Medium | Low | Artifact rows hold a copy of the blob (content-addressed); the import doesn't soft-link. Lineage records the source doc_id but the file in `inputs/` is independent. Documented as a feature: "deleting a Library doc doesn't break in-flight project work." |

---

## Sequencing notes

* **Phase 0 must ship before Phase 1** — duh, but worth saying:
  the data model + filesystem layout + auth model are foundations
  everything else depends on. Don't try to interleave.
* **Phase 1 can ship without Phase 2** — the Workspace is useful
  as a manual file manager + chat scope even with no agent
  capability. This is the "demo a customer the architecture"
  milestone.
* **Phase 2 + Phase 3 are tightly coupled** — `python_exec` alone
  is fun but limited; pairing it with `read_file` / `write_file`
  / `import_from_library` is when the agent becomes useful. Treat
  them as a single shipping unit.
* **Phase 4 is the most complex by far.** Plan-Execute-Reflect +
  HITL + sub-agent fan-out + crash-resilient state machine = ~4
  weeks of someone's full attention. Don't underbudget.

---

## Glossary

* **Library** — the indexed corpus; current Workspace renamed.
* **Workspace** — the new agent-output surface (this doc's subject).
* **Project** — a container under Workspace owning a workdir,
  conversations, agent runs, and artifacts.
* **Artifact** — a file produced by an agent step, with lineage.
* **ExecutionSession** — a per-project Docker container running a
  long-lived Python kernel.
* **AgentRun** — one execution of the orchestrator over a user
  task; produces a plan and a sequence of steps.
* **HITL** — Human In The Loop. Plan-approval gates, mid-run
  intervention, manual artifact review.
