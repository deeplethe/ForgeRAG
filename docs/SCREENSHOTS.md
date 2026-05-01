# Screenshot guide

This is the canonical shot list referenced from the README. Drop captured PNGs into `docs/screenshots/` using the exact filenames below — the README and any future docs will pick them up without further edits.

> **Why this lives here, not in the repo as binaries:** screenshots churn fast (every Workspace/Chat/KG polish cycle invalidates them), but the shot list is stable. Capture locally before publishing a release; commit the PNGs in a separate "docs: refresh screenshots" commit so binary diffs don't pollute feature commits.

## How to capture

1. Run `python main.py` and `cd web && npm run dev` in two terminals.
2. Open `http://localhost:5173` in a 1440×900 Chrome window (DevTools closed).
3. Have at least 5 docs ingested (mix of PDF + DOCX + MD), one with a non-trivial KG, one mid-ingestion to capture the in-flight chip.
4. Use the OS native screenshot tool (Win+Shift+S / Cmd+Shift+4) — no DevTools device toolbar; we want crisp 1× pixels.
5. Crop to remove the address bar but keep app chrome.
6. Save as PNG. Don't bother optimizing — keep the originals; CI can run `pngcrush` later.

## Shot list

| Filename | What it shows | Tips |
|---|---|---|
| `workspace_grid.png` | Workspace grid view, ≥6 docs across 2 folders, one mid-ingest with the amber spinner chip | Hover one card so the action menu pip is visible. Make sure folder breadcrumb shows depth ≥ 2. |
| `workspace_list.png` | Same workspace in list view, one row drag-highlighted | Start a drag and screenshot mid-motion (the white drop indicator). |
| `chat_streaming.png` | Mid-stream answer with `[c_1] [c_2]` citations rendering, retrieval trace panel open in the gutter | Use a question that triggers ≥3 paths (BM25 + Vector + KG). |
| `chat_citation_open.png` | Click `[c_2]` → PDF viewer pops the source page with the bbox highlighted | Pick a citation that lands inside a table or figure for visual contrast. |
| `doc_detail_three_pane.png` | DocDetail page: tree navigator (left) + PDF viewer (center) + chunks/KG mini panel (right) | Hover a chunk so its source bbox is highlighted on the PDF. |
| `kg_full.png` | Knowledge Graph tab with Sigma rendering, ≥80 nodes, force-directed layout settled | Filter by 1 doc so the graph isn't a hairball. Click an edge so the supporting-chunk panel is visible. |
| `kg_zoom.png` | KG zoomed into a single entity's neighborhood, edge tooltip showing the relation description | Pick an entity with 5–10 neighbors. |
| `recycle_bin.png` | Workspace `/__trash__/` view with restored badge + "30 days until purge" hint | Soft-delete 2 items first; one should have a missing-parent restore tooltip. |
| `settings_panel.png` | Settings drawer with MinerU toggled on + provider list | Mask any real API keys before committing — paste over the input with a placeholder before screenshotting. |
| `retrieval_trace.png` | Per-query trace panel: BM25 hits, vector hits, KG hits, rerank drops, RRF scores | Use a meaty query so all paths contributed. |

## Hero shot (homepage / OG image)

For the GitHub repo's social preview and the future landing page:

- **Resolution:** 1280×640 (GitHub's OG size).
- **Composition:** Workspace on the left half (cropped to 2 folders + 4 cards), Chat on the right half (cropped to one streaming answer with a visible citation popping the PDF). A subtle gradient seam down the middle.
- **Filename:** `og_hero.png` — drop in `docs/screenshots/` and reference from `README.md` if/when we want a hero band above the title.

## Updating the README

Once captured, the README's "📸 What you get" table can be enriched with inline images:

```md
| **Workspace** | ![](docs/screenshots/workspace_grid.png) |
```

Keep the table — the prose row already explains the feature; the image row is pure visual proof.

## Don't capture

- **Personal data.** Any document title that's a real client/internal filename — re-ingest a public PDF (e.g., a Wikipedia export, an arxiv paper) before the screenshot run.
- **API keys** in any UI panel. Settings drawer, error toasts, anywhere. Mask before capture.
- **Browser bookmarks bar / extensions.** Use a clean profile or hide the bar with `Ctrl+Shift+B`.
