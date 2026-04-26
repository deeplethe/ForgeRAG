<template>
  <!--
    Boolean override row for the Simulation params panel.
    Originally a tri-state ("default | on | off") — the "default" was removed
    once the form started preloading from backend cfg, so this is now just a
    proper iOS-style switch (Toggle component) with a reset-to-default button
    that appears once the user has changed the value.
  -->
  <div class="row">
    <span class="label">
      {{ label }}
      <button
        v-if="modified"
        class="reset-btn"
        title="Reset to default"
        @click.stop="$emit('reset')"
      >↺</button>
    </span>
    <Toggle
      :model-value="!!modelValue"
      @update:modelValue="$emit('update:modelValue', $event)"
    />
  </div>
</template>

<script setup>
import Toggle from '@/components/Toggle.vue'

defineProps({
  label: String,
  modelValue: { default: null },
  modified: { type: Boolean, default: false },
})
defineEmits(['update:modelValue', 'reset'])
</script>

<style scoped>
.row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 4px 0;
}
.label {
  flex: 1;
  min-width: 0;
  font-size: 11px;
  color: var(--color-t2);
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.reset-btn {
  font-size: 10px;
  color: var(--color-t3);
  background: transparent;
  border: none;
  cursor: pointer;
  padding: 0 4px;
  line-height: 1;
}
.reset-btn:hover { color: var(--color-t1); }
</style>
