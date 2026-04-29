<template>
  <!-- Single-row toolbar: [breadcrumb slot] ... [actions on the right].
       Breadcrumb arrives via the ``lead`` slot so the parent owns nav
       state without us re-passing crumbs through props. Padding
       (px-5 py-3) matches the Knowledge page topbar so the global
       page-header height stays consistent across views. -->
  <div class="flex items-center gap-1 px-5 py-3 border-b border-line bg-bg2">
    <slot name="lead" />

    <div class="flex-1"></div>

    <!-- Primary actions — moved to the right cluster so they sit next
         to search/view/trash instead of sandwiching the breadcrumb.
         Folder emoji + explicit "New folder" label avoids the
         "what does ⊕ create?" ambiguity (an upload-and-create-doc
         flow lives next to it). -->
    <button class="toolbar-btn" @click="$emit('new-folder')" title="New folder (Ctrl+N)">
      📁 <span>New folder</span>
    </button>
    <button class="toolbar-btn" @click="$emit('upload')" title="Upload file">
      ⬆ <span>Upload</span>
    </button>

    <!-- Search — filters the current folder's children -->
    <div class="search-wrap ml-2">
      <span class="search-icon">⌕</span>
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
      >✕</button>
    </div>

    <!-- View mode switcher -->
    <div class="flex items-center gap-0.5 p-0.5 border border-line rounded-md ml-2">
      <button
        class="view-btn"
        :class="{ 'view-btn--active': viewMode === 'grid' }"
        @click="$emit('set-view', 'grid')"
        title="Grid view (Ctrl+1)"
      >⊞</button>
      <button
        class="view-btn"
        :class="{ 'view-btn--active': viewMode === 'list' }"
        @click="$emit('set-view', 'list')"
        title="List view (Ctrl+2)"
      >☰</button>
    </div>

    <!-- Trash shortcut -->
    <button
      class="toolbar-btn ml-2"
      @click="$emit('show-trash')"
      title="Recycle bin"
    >
      🗑 <span v-if="trashCount">{{ trashCount }}</span>
    </button>
  </div>
</template>

<script setup>
defineProps({
  viewMode: { type: String, required: true },
  trashCount: { type: Number, default: 0 },
  search: { type: String, default: '' },
})
defineEmits(['new-folder', 'upload', 'set-view', 'show-trash', 'update:search'])
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
.toolbar-btn:hover {
  background: var(--color-bg2);
  color: var(--color-t1);
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
  left: 8px;
  font-size: 12px;
  color: var(--color-t3);
  pointer-events: none;
  line-height: 1;
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
