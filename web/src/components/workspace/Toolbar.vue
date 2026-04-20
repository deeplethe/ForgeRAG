<template>
  <div class="flex items-center gap-1 px-3 py-1.5 border-b border-line bg-bg">
    <!-- Primary actions — everything else lives in the context menu -->
    <button class="toolbar-btn" @click="$emit('new-folder')" title="New folder (Ctrl+N)">
      ⊕ <span>New</span>
    </button>
    <button class="toolbar-btn" @click="$emit('upload')" title="Upload file">
      ⬆ <span>Upload</span>
    </button>

    <div class="flex-1"></div>

    <!-- View mode switcher -->
    <div class="flex items-center gap-0.5 p-0.5 border border-line rounded-md">
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
      <button
        class="view-btn"
        :class="{ 'view-btn--active': viewMode === 'miller' }"
        @click="$emit('set-view', 'miller')"
        title="Miller columns (Ctrl+3)"
      >▦</button>
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
})
defineEmits(['new-folder', 'upload', 'set-view', 'show-trash'])
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
</style>
