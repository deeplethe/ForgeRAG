<template>
  <!-- Single-row toolbar: [breadcrumb slot] ... [actions on the right].
       Breadcrumb arrives via the ``lead`` slot so the parent owns nav
       state without us re-passing crumbs through props. Padding
       (px-5 py-3) matches the Knowledge page topbar so the global
       page-header height stays consistent across views. -->
  <!-- ``min-h-[52px]`` locks the toolbar height so it doesn't shrink
       2px when switching to trash mode. In browse mode the search
       input (the tallest action) drives the natural height; in trash
       mode there's no search, so without this min-height the
       toolbar would collapse to whatever the breadcrumb + small
       buttons need, causing visible vertical jitter on enter/exit. -->
  <div class="flex items-center gap-1 px-5 py-3 border-b border-line bg-bg2 min-h-[52px]">
    <slot name="lead" />

    <div class="flex-1"></div>

    <!-- Browse-mode actions: hidden in trash because none of them
         (create / upload / search / view-toggle / open-trash) make
         sense while the user is looking at deleted items. -->
    <template v-if="!viewingTrash">
      <button class="toolbar-btn" @click="$emit('new-folder')" title="New folder (Ctrl+N)">
        <FolderPlus :size="14" :stroke-width="1.5" />
        <span>New folder</span>
      </button>
      <button class="toolbar-btn" @click="$emit('upload')" title="Upload file">
        <Upload :size="14" :stroke-width="1.5" />
        <span>Upload</span>
      </button>

      <!-- Search — filters the current folder's children -->
      <div class="search-wrap ml-2">
        <Search class="search-icon" :size="14" :stroke-width="1.5" />
        <input
          :value="search"
          @input="$emit('update:search', $event.target.value)"
          placeholder="Search this folder…"
          class="search-input"
        />
        <button
          v-if="search"
          class="search-clear"
          @click="$emit('update:search', '')"
          title="Clear"
        ><X :size="12" :stroke-width="1.5" /></button>
      </div>

      <!-- View mode switcher -->
      <div class="flex items-center gap-0.5 p-0.5 border border-line rounded-md ml-2">
        <button
          class="view-btn"
          :class="{ 'view-btn--active': viewMode === 'grid' }"
          @click="$emit('set-view', 'grid')"
          title="Grid view (Ctrl+1)"
        ><LayoutGrid :size="14" :stroke-width="1.5" /></button>
        <button
          class="view-btn"
          :class="{ 'view-btn--active': viewMode === 'list' }"
          @click="$emit('set-view', 'list')"
          title="List view (Ctrl+2)"
        ><List :size="14" :stroke-width="1.5" /></button>
      </div>

      <!-- Trash shortcut -->
      <button
        class="toolbar-btn ml-2"
        @click="$emit('show-trash')"
        title="Recycle bin"
      >
        <Trash2 :size="14" :stroke-width="1.5" />
        <span v-if="trashCount">{{ trashCount }}</span>
      </button>
    </template>

    <!-- Trash-mode actions. Order matters: the rightmost slot in
         browse-mode was the bin icon, so the rightmost slot here is
         the *back-to-workspace* arrow — clicking the same screen
         position you arrived from feels intuitive and avoids accidentally
         hitting Empty bin (a destructive action). The Empty bin button
         lives further left and uses neutral styling; the destructive
         intent is gated by the confirmation modal. -->
    <template v-else>
      <span class="text-[11px] text-t3">
        {{ trashCount }} item{{ trashCount === 1 ? '' : 's' }}
      </span>
      <button
        class="toolbar-btn toolbar-btn--danger ml-2"
        :disabled="!trashCount || emptyingTrash"
        @click="$emit('empty-trash')"
        title="Permanently delete every item in the recycle bin"
      >{{ emptyingTrash ? 'Emptying…' : 'Empty bin' }}</button>
      <button
        class="toolbar-btn ml-2"
        @click="$emit('exit-trash')"
        title="Back to workspace"
      >
        <ArrowLeft class="w-3.5 h-3.5" :stroke-width="1.5" />
      </button>
    </template>
  </div>
</template>

<script setup>
import { ArrowLeft, FolderPlus, LayoutGrid, List, Search, Trash2, Upload, X } from 'lucide-vue-next'

defineProps({
  viewMode: { type: String, required: true },
  trashCount: { type: Number, default: 0 },
  search: { type: String, default: '' },
  viewingTrash: { type: Boolean, default: false },
  // While Empty bin is in flight (slow vector + KG + relational purge),
  // the parent flips this on; the button disables to prevent a double
  // fire and the label flips to communicate progress.
  emptyingTrash: { type: Boolean, default: false },
})
defineEmits(['new-folder', 'upload', 'set-view', 'show-trash', 'update:search', 'empty-trash', 'exit-trash'])
</script>

<style scoped>
.toolbar-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  font-size: 11px;
  color: var(--color-t2);
  background: transparent;
  border: 1px solid transparent;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.toolbar-btn:hover:not(:disabled) {
  background: var(--color-bg2);
  color: var(--color-t1);
}
.toolbar-btn:disabled { opacity: 0.4; cursor: not-allowed; }

/* Secondary-destructive variant — red text + visible neutral border
   so the button reads as a button at rest, with a red-tinted hover
   that surfaces the destructive intent only when the user reaches
   for it. Matches Vercel's outline-destructive pattern; the actual
   irreversible action is gated by the confirmation modal. */
.toolbar-btn--danger {
  color: var(--color-err-fg, #dc2626);
  border-color: var(--color-line);
}
.toolbar-btn--danger:hover:not(:disabled) {
  background: color-mix(in srgb, var(--color-err-fg, #dc2626) 10%, transparent);
  border-color: var(--color-err-fg, #dc2626);
  color: var(--color-err-fg, #dc2626);
}

.view-btn {
  width: 26px;
  height: 22px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  color: var(--color-t3);
  background: transparent;
  border-radius: 4px;
  border: none;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.view-btn:hover { color: var(--color-t1); background: var(--color-bg2); }
.view-btn--active { background: var(--color-bg3); color: var(--color-t1); }

/* Search field — inline icon + clear button */
.search-wrap {
  position: relative;
  display: flex;
  align-items: center;
  width: 240px;
}
.search-icon {
  position: absolute;
  left: 7px;
  width: 14px;
  height: 14px;
  color: var(--color-t3);
  pointer-events: none;
}
.search-input {
  width: 100%;
  padding: 5px 26px 5px 24px;
  font-size: 11px;
  color: var(--color-t1);
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: var(--r-sm);
  outline: none;
  transition: border-color 0.12s, box-shadow 0.12s;
}
.search-input:hover { border-color: var(--color-line2); }
.search-input:focus { border-color: var(--color-line2); box-shadow: var(--ring-focus); }
.search-input::placeholder { color: var(--color-t3); }
.search-clear {
  position: absolute;
  right: 4px;
  width: 18px;
  height: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  color: var(--color-t3);
  background: transparent;
  border: none;
  border-radius: 3px;
  cursor: pointer;
}
.search-clear:hover { background: var(--color-bg2); color: var(--color-t1); }
</style>
