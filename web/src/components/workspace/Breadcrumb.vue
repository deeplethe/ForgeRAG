<template>
  <nav class="flex items-center gap-1 text-xs text-t2 select-none overflow-x-auto">
    <template v-for="(c, i) in crumbs" :key="c.path">
      <span v-if="i > 0" class="text-t3">›</span>
      <button
        class="px-2 py-1 rounded hover:bg-bg2 hover:text-t1 transition-colors whitespace-nowrap"
        :class="{ 'text-t1 font-medium': i === crumbs.length - 1 }"
        @click="$emit('navigate', c.path)"
      >
        {{ c.name }}
      </button>
    </template>
  </nav>
</template>

<script setup>
defineProps({
  crumbs: { type: Array, required: true },
})
defineEmits(['navigate'])
</script>
