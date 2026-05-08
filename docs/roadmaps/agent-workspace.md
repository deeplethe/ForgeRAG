# Roadmap: Agent Workspace (Multi-Agent Production System)

**Status:** Design — Phase 0 not yet started
**Last updated:** 2026-05-08

This is the largest feature on OpenCraig's roadmap. It moves the
product from "find the answer in your docs and cite the page" to
"have an agent do the actual work — read files, run code, write
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

**Chosen.** A Project owns: workdir on disk, single-user owner,
artifacts, agent runs, execution sessions. A Chat Conversation
*belongs to* a Project (via FK). Multiple conversations of the
**same user** under one project share the same workdir + artifacts;
projects are not shared between users (see "Project = single-user"
in the Settled decisions).

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
   owner_id              (the SOLE access-control field — projects are
                          single-user; no shared_with, no member rows)
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

| Tool | Phase | What it does |
|---|---|---|
| `search_library` | already exists | Retrieves chunks from the Library (existing RAG) |
| `read_chunk` | already exists | Pulls a specific chunk by ID |
| `read_tree` | already exists | Document outline navigation |
| **`python_exec`** | 2 | Run Python in the project's sandbox |
| **`bash_exec`** | 2 | Run shell in the project's sandbox |
| **`install_runtime`** | 2 | Install a non-Python language runtime (R / Julia / Node / Rust / …) into the user's persistent `/workspace/.envs/` volume on first need; registers `<lang>_exec` for the rest of the run |
| **`import_from_library`** | **2** | Copy a Library doc the user has read access to into the project workdir's `inputs/` (records an Artifact with `lineage.source = chunk/doc reference`). **Promoted from Phase 3** — agent must be able to pull files into its workdir at the same moment it gains code execution, otherwise Q&A turns "find me X and analyze it" into a hand-paste step the user shouldn't have to do |
| **`list_files`** | 3 | Glob the project workdir |
| **`read_file`** | 3 | Read a file from the project workdir |
| **`write_file`** | 3 | Write a file (records an Artifact) |
| **`promote_to_library`** | 3 | Push an artifact back into the Library (becomes indexed) |
| `web_search` | 5 | External web search via Tavily/Brave/SearXNG |
| `fetch_url` | 5 | Download + parse a URL into markdown |

---

## Phased plan (high-level — see Phase 0 / Phase 1 detail below)

| Phase | What ships | Demo capability |
|---|---|---|
| **0** | Rename Workspace → Library; new empty Workspace surface; data model; placeholder Project CRUD (single-user) | "We have two surfaces now" — just architecture |
| **1** | Workspace UI = file manager over project workdir; manual "import from Library" UI; Chat ↔ Project binding | User can create a Project, pull a Library doc into it, open a chat against it |
| **2** | `python_exec` + `bash_exec` + `install_runtime` + **`import_from_library`** via per-user Docker container (userns-remap) + `jupyter_client`; rich-output rendering; sandbox image with ~25 CLI tools pre-installed; per-user `.envs/` volume for lazy R/Julia/Node | "Find Q3 sales and analyze it" works end-to-end (Library search → import → python_exec → chart) |
| **3** | Local file I/O tools (`list_files` / `read_file` / `write_file`) + `promote_to_library` | Agent freely manipulates project workdir; can push artifacts back to Library |
| **4** | Plan-Execute-Reflect orchestrator; long-running runs; HITL gates | "Compare these 5 contracts and produce a tracker xlsx" works |
| **5** | Web search (default-on) + fetch_url + injection defense | Agents can pull external sources |
| **6** | Artifact lineage UI; project export; cost dashboard | Sale-ready polish (no built-in templates — operator drives use cases) |

Phase 0 + Phase 1 are designed to be **3 weeks total**. The rest
is 12-18 additional weeks depending on team size.

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
| **Project = single-user, Library = shared** | Projects are personal workbenches: one owner, no sharing, no member management. Library keeps its existing folder-grant multi-user model. Rationale: a project owns the agent's live kernel state + run history + intermediate artifacts; multi-writer on those would create races + state pollution with no real collaboration upside. Users collaborate by **sharing Library content**, then each runs their own agent in their own project against the same shared knowledge. |
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
  - `GET    /api/v1/projects`               — list the caller's projects
  - `POST   /api/v1/projects`               — create
  - `GET    /api/v1/projects/{id}`          — detail
  - `PATCH  /api/v1/projects/{id}`          — rename / edit description
  - `DELETE /api/v1/projects/{id}`          — soft-delete
* All routes are owner-only (admin bypass for global ops). 404 on
  no-access — same code as a missing project, to avoid existence
  confirmation. Projects are single-user; no `/members` endpoints.
* Audit log: `project.create / project.rename / project.delete /
  project.update`.

### 0.5 — Frontend Workspace skeleton  (~1 day)

* `web/src/views/Workspace.vue` (NEW — different from the renamed
  one): empty-state shows "No projects yet" + a "Create project"
  button.
* New API client `web/src/api/projects.js` — list / create / etc.
* Sidebar link "Workspace" → `/workspace` (the new route).
* Project list page: simple grid of cards `(name, description,
  last_active)`; click → project detail page.
* Project detail page: just shows name, description, "no chats
  yet" placeholder. No member dialog — projects are single-user.

### 0.6 — Tests + smoke  (~0.5 day)

* `tests/test_route_projects.py`: CRUD + ownership boundary
  (alice's project is invisible to bob; bob can't read / edit /
  delete it).
* `tests/test_no_phone_home.py`: no new SDKs.
* Manual smoke: register two users, create a project as A, verify
  B cannot list / open / mutate it.

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
* **No project sharing.** Projects are single-user by design — see
  the "Project = single-user, Library = shared" decision. Users
  collaborate via shared Library content, not shared projects.

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
| Cost runaway from agentic loops | High | High | Phase 0 already adds `total_cost_usd` column; Phase 4 adds per-project budget cap + plan-time cost preview |
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
