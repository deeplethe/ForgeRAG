# Demo video shoot guide

Why a video for OpenCraig specifically: the core value prop — *"every claim grounded back to a page + bbox"* — is impossible to convey in static screenshots. A 60-second clip showing a citation click jumping the PDF to the highlighted region is worth more than the entire features list.

This guide is opinionated. Follow it unless you have a strong reason not to.

## TL;DR — three videos to ship

| Video | Length | Where it goes | Status |
|---|---|---|---|
| **`hero_60s.mp4`** | 60s, no narration | README badge band, GitHub repo "Featured" video, Twitter/X | Highest priority |
| **`walkthrough_3min.mp4`** | 3min, narrated | YouTube, linked from README under "Why OpenCraig" | Next |
| **`kg_30s.gif`** | 30s, looping | README highlights row | Nice-to-have |

Skip narration on the hero — most viewers watch muted on autoplay. Use captions instead.

---

## 1. The hero (60s, no narration)

**Goal:** in 60 seconds, prove the *one thing* every other RAG can't: a click on a citation lands you on the exact bbox in the source PDF.

### Story beat (target ~10s per beat, six beats)

1. **0–10s — Workspace.** Drag a PDF onto the Workspace. The card appears with the amber in-flight chip. Time-cut to ~3s of pipeline progress (parsing → embedding → building graph). Card flips to ✓ ready.
2. **10–20s — Chat.** Type a question that visibly needs multi-hop reasoning, e.g. *"Compare the Q3 supplier risk to Q2 — which suppliers shifted?"*. Press Enter.
3. **20–30s — Streaming.** Tokens stream. The retrieval trace gutter populates: BM25 hits, Vector hits, KG hits, Tree-nav hits. Each path "lights up" as it returns. (This is the money shot — the multi-path fusion is invisible in screenshots, but mesmerizing in motion.)
4. **30–40s — Citations.** Answer finishes with `[c_1] [c_3] [c_5]` markers. Hover one — tooltip preview. Click — PDF viewer slides in, page jumps, **bbox highlights**.
5. **40–50s — KG.** Cut to Knowledge Graph tab. Sigma graph renders. Filter by the doc just ingested. Click an edge → supporting-chunk drawer opens.
6. **50–60s — Outro.** Logo, URL, one-liner: *"RAG that thinks like a domain expert."*

### Capture setup

- **Resolution:** 1920×1080, 60 fps. Anything less and the citation-bbox text becomes mush.
- **Browser window:** 1440×900 inside a 1920×1080 canvas with a 1px subtle border, dark grey background. Don't full-screen — the chrome looks more "real product" with a window frame.
- **Recording tool:** OBS (free) with the Display Capture source. NOT browser-extension recorders — they drop frames during heavy renders (Sigma, the streaming gutter).
- **Mouse cursor:** enable a soft yellow click highlight in OBS. Otherwise viewers can't see what's being clicked.
- **Cursor speed:** slow down. Aim for 1 click per second visible to the viewer; you can always cut later.

### Captions (essential, since muted)

Hard-burn the captions in the editor. One short line per beat:

| Time | Caption |
|---|---|
| 0–3s | `Drop a PDF.` |
| 3–9s | `OpenCraig parses, builds a tree, embeds, and extracts a knowledge graph.` |
| 10–18s | `Ask anything.` |
| 18–28s | `4 retrieval paths run in parallel: BM25, vector, knowledge graph, tree navigation.` |
| 28–38s | `Every claim links to its source — page and bounding box.` |
| 38–48s | `Knowledge graph for cross-document reasoning.` |
| 50–60s | `OpenCraig — github.com/opencraig/opencraig` |

### Editing

- **Cuts, not transitions.** Hard cuts every beat. Crossfades feel marketing-y; cuts feel like a developer demoing.
- **Speed up the boring middles.** The pipeline progress (5s real time → 1s in the cut) is fine to time-warp. The citation click is *not* — keep it real-time so viewers register what just happened.
- **No background music in the hero.** It's 60 seconds; let the UI speak. The 3-minute walkthrough can have music.

### Export

- `H.264, CRF 18, 1080p60` — quality fine for GitHub's 100MB cap (60s @ CRF 18 lands ~30–50MB).
- **Also export a 5MB GIF** at 720p10 for places that don't autoplay video (some Markdown renderers, some social embeds).

---

## 2. The walkthrough (3 minutes, narrated)

For the audience that watched the hero and wants to know if it's real.

### Outline

| Section | Time | What to show |
|---|---|---|
| **Why OpenCraig** | 0:00–0:30 | Voiceover: the comparison table from the README, narrated over a still. Naive RAG misses, GraphRAG hallucinates, PageIndex doesn't scale. |
| **Setup** | 0:30–0:50 | `git clone` → `python scripts/setup.py` (sped up 10×) → wizard finishing → `python main.py`. Prove "one command, no Docker required". |
| **Ingest** | 0:50–1:30 | Drop 3 docs of different formats (PDF, DOCX, MD). Show the in-flight chips. While they parse, narrate the pipeline (parse → tree → chunk → embed → KG). |
| **Query** | 1:30–2:30 | One simple query (BM25 + vector dominate), one structural query (tree-nav lights up), one cross-document query (KG lights up). Click citations each time. |
| **Trust the answer** | 2:30–2:50 | Show the retrieval trace panel — *what got retrieved, what got reranked, what got dropped*. This is the "we're not hiding anything" moment. |
| **Outro** | 2:50–3:00 | "Star us on GitHub. Discord link. Roadmap link." Logo + URL. |

### Narration tips

- **Write the script first, time it with a stopwatch, then re-record over the existing footage.** Recording narration first leads to footage that doesn't fit.
- **Talk like a developer to developers.** No "Imagine if..." or "Have you ever struggled with...". Just: "OK, dropping a PDF in. Notice the chip — that's the ingestion pipeline running. Each stage is a separate retry boundary..."
- **Pause more than feels natural.** Viewers need a beat to read the UI. The first cut will feel too slow; trust it.

### Background music

- One soft loop, mixed at -28 dB so the voiceover sits clearly above. Royalty-free; check `youtube.com/audiolibrary`.

---

## 3. The KG loop (30s GIF)

A small looping GIF for the README's "Highlights" section. No story — pure eye-candy of the Sigma graph settling into its force-directed layout, then a click-to-explore.

- **Capture:** 30s of: graph initially clustered, force layout running, settled, user clicks a high-degree node, neighborhood expands.
- **Export:** 720p, 10 fps GIF (≤5MB).
- **Filename:** `kg_30s.gif` in `docs/screenshots/`.

---

## Where to host

| Asset | Where | Notes |
|---|---|---|
| Hero `.mp4` | Commit to `docs/videos/hero_60s.mp4` *only if ≤25MB*. Otherwise upload to GitHub Releases as a release asset (no LFS — most contributors don't have it set up). | GitHub renders MP4s inline in README. |
| Walkthrough `.mp4` | YouTube — upload as **unlisted first**, embed via thumbnail link in README, flip to public after a sanity-check pass. | Lets you swap the video without changing the README. |
| KG `.gif` | Commit to `docs/screenshots/kg_30s.gif`. | GIFs render inline everywhere. |

GitHub LFS is tempting but adds friction for every cloner; only switch to it if videos exceed Releases' 2GB-per-file limit, which they won't.

---

## Pre-flight checklist (before recording)

- [ ] Ingested ≥5 docs, including at least one with a meaty KG (≥50 entities) and one mid-ingestion to catch the chip.
- [ ] No real client filenames in the workspace — replace with public PDFs (arxiv, Wikipedia exports).
- [ ] No real API keys visible anywhere (Settings drawer especially).
- [ ] DevTools closed. Browser bookmarks bar hidden (`Ctrl+Shift+B`).
- [ ] Screen recorder set to 60 fps, not 30.
- [ ] Mouse cursor highlighting on.
- [ ] All notifications silenced (Slack, Mail, calendar).
- [ ] Dark mode chosen consistently — pick one and stick to it; switching mid-shoot is jarring.

---

## Don't

- **Don't fake the latency.** If a query takes 4 seconds, show 4 seconds (or speed-warp the whole clip uniformly — never just the slow parts). Viewers can smell edited timings.
- **Don't narrate every click.** "Now I'm clicking on the chat tab" is filler. Trust the visual.
- **Don't add zoom-ins on every UI element.** One zoom on the citation-bbox highlight is fine. Three zooms in a 60-second video reads as "I'm padding."
