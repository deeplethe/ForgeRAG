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

**Chosen.** A Project owns: workdir on disk, member grants,
artifacts, agent runs, execution sessions. A Chat Conversation
*belongs to* a Project (via FK). Multiple conversations under one
project share the same workdir + artifacts.

**Rejected: artifacts attached directly to Conversation.** Cleaner-
sounding but breaks the moment a user wants two conversations on
the same project ("ask the agent to clean the data" then later
"ask it to plot the cleaned data" — same workdir, different chat
threads). Project is the persistent unit; conversations come and
go.

### 3. One sandbox container per Project execution session

**Chosen.** Each Project gets a dedicated Docker container running
a long-lived Python kernel. Variables, imports, opened files
persist across `python_exec` calls within the same conversation
turn AND across turns. Container is reaped after 10 min of
inactivity; resurrected on next call (state lost — checkpoint via
explicit file writes).

**Rejected: one container per `python_exec` call.** Cold-start
overhead (~2-3s per call) wrecks the agent flow when an agent
runs 10-20 code blocks per task.

**Rejected: shared kernel pool across projects.** Memory pressure
+ cross-tenant data leak risk. Project isolation is the security
boundary; sharing kernels would punch through it.

**Rejected: bare-process subprocess sandboxing (firejail /
bubblewrap).** Linux-only; doesn't run on macOS / Windows dev
boxes; weaker than container-based isolation against
sophisticated escape attempts.

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
   owner_id
   created_at
   updated_at
   shared_with (jsonb)   ← reuses Folder.shared_with shape

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
└── kernels/                         # NEW — per-execution-session state
    └── <session_id>/                  (mounts back into project workdir)
```

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
| **`list_files`** | 3 | Glob the project workdir |
| **`read_file`** | 3 | Read a file from the project workdir |
| **`write_file`** | 3 | Write a file (records an Artifact) |
| **`import_from_library`** | 3 | Copy / link a Library doc into the project workdir |
| **`promote_to_library`** | 3 | Push an artifact back into the Library (becomes indexed) |
| `web_search` | 5 | External web search via Tavily/Brave/SearXNG |
| `fetch_url` | 5 | Download + parse a URL into markdown |

---

## Phased plan (high-level — see Phase 0 / Phase 1 detail below)

| Phase | What ships | Demo capability |
|---|---|---|
| **0** | Rename Workspace → Library; new empty Workspace surface; data model; placeholder Project CRUD | "We have two surfaces now" — just architecture |
| **1** | Workspace UI = file manager over project workdir; Project sharing; Chat ↔ Project binding | User can create a Project, upload files, open a chat against it |
| **2** | Sandboxed `python_exec` tool; Docker-based kernel pool; rich-output rendering | "Analyze this CSV and plot it" works |
| **3** | File I/O tools + Library bridge | "Take that contract from Library, extract X" works |
| **4** | Plan-Execute-Reflect orchestrator; long-running runs; HITL gates | "Compare these 5 contracts and produce a tracker xlsx" works |
| **5** | Web search + fetch_url + injection defense | Agents can pull external sources |
| **6** | Artifact lineage UI; project export; templates; cost dashboard | Sale-ready polish |

Phase 0 + Phase 1 are designed to be **3 weeks total**. The rest
is 12-18 additional weeks depending on team size.

---

## Open decisions (asked of operator before starting)

1. **Sandbox** — Docker per-project (chosen above) confirmed?
2. **First demo use case** — needed to focus Phase 2-4 work
3. **Web search default** — on or off out of the box?
4. **Multi-user from day 1, or single-user MVP first?**
5. **Built-in templates** — which 3-5 to ship?

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
  - `GET    /api/v1/projects`               — list (filtered by grants)
  - `POST   /api/v1/projects`               — create
  - `GET    /api/v1/projects/{id}`          — detail
  - `PATCH  /api/v1/projects/{id}`          — rename / edit description
  - `DELETE /api/v1/projects/{id}`          — soft-delete
  - `GET    /api/v1/projects/{id}/members`  — list members
  - `POST   /api/v1/projects/{id}/members`  — invite (reuses email lookup)
  - `PATCH  /api/v1/projects/{id}/members/{user_id}` — change role
  - `DELETE /api/v1/projects/{id}/members/{user_id}` — remove
* All routes admin-and-owner gated through a `_require_project_access`
  helper, mirroring `_require_share_permission` in folders.py.
* Audit log: `project.create / project.rename / project.delete /
  project.share / project.unshare / project.update_role`.

### 0.5 — Frontend Workspace skeleton  (~1 day)

* `web/src/views/Workspace.vue` (NEW — different from the renamed
  one): empty-state shows "No projects yet" + a "Create project"
  button.
* New API client `web/src/api/projects.js` — list / create / etc.
* Sidebar link "Workspace" → `/workspace` (the new route).
* Project list page: simple grid of cards `(name, description,
  last_active, owner_avatar)`; click → project detail page.
* Project detail page: just shows name, description, "no chats
  yet" placeholder. Member management dialog reuses
  `FolderMembersDialog.vue` adapted for projects (or a
  near-identical clone — refactoring later).

### 0.6 — Tests + smoke  (~0.5 day)

* `tests/test_project_routes.py`: CRUD + share happy path.
* `tests/test_no_phone_home.py`: no new SDKs.
* Manual smoke: register two users, create a project as A, share
  to B, verify B sees it.

## Phase 0 acceptance checklist

- [ ] Sidebar shows two distinct entries: "Library" + "Workspace"
- [ ] `/library` renders the existing folder-tree UI (same
      functionality, just rebranded)
- [ ] `/workspace` renders an empty project list
- [ ] Create-project button works; project appears in the list
- [ ] Project's workdir actually exists on disk under
      `storage/projects/<id>/`
- [ ] Sharing a project with another user grants them list/read
      access; revoking removes it
- [ ] All existing tests still pass; build succeeds
- [ ] No Library-related functionality is broken by the rename

---

# Phase 1 — Workspace as a project file manager + chat binding

**Goal:** Phase 0 has the architecture; Phase 1 makes the
Workspace **useful** as a project-scoped file manager (without any
agent capability yet). The user can manually upload files to a
project, organize them, and open a chat scoped to a project — at
which point retrieval still pulls from the Library, but the chat
is *associated with* the project so future agent work has somewhere
to land.

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

### 1.4 — Project member management UI polish  (~2 days)

* Refactor `FolderMembersDialog.vue` → `MembersDialog.vue` with a
  `kind: 'folder' | 'project'` prop, since both share 95% of the
  flow. Drives both Library and Workspace member edits.
* Workspace home page: show "Shared with you" projects in a
  separate section from "Your projects" so the ownership/scope
  is obvious at a glance.

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
* End-to-end smoke: alice creates project, uploads a CSV, shares
  to bob, bob downloads the CSV, bob opens chat-in-project,
  conversation row carries `project_id`.

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
- [ ] Sharing a project propagates file access to the invited user
- [ ] Chat opened from a project is bound to it; conversation row
      has `project_id` set
- [ ] Workspace home shows "Your projects" + "Shared with you"
      sections distinctly
- [ ] All Library functionality (search, KG, ingestion, members)
      works exactly as before — Workspace doesn't bleed into it
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
* **No Library bridge.** Files in projects are uploaded by the
  user manually; importing a Library doc into a project workdir
  is Phase 3.
* **No artifact concept.** The `artifacts` table exists from 0.2
  but no rows get written until Phase 2's `python_exec` produces
  files (and Phase 3's `write_file` formalises Artifact creation).
* **No agent runs.** `agent_runs` table exists; remains empty
  through Phase 0-3.
* **No project-level RBAC subtleties** (e.g. "this user can see
  files but not run agents"). One role per project member: `r`
  or `rw`, same as Library. Phase 7+ refines if customers ask.

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
| Multi-user permission edge cases on shared projects | Medium | Medium | Phase 1 reuses Library's tested grant model 1:1; new edge cases emerge in Phase 4 with sub-agents (deferred) |

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
