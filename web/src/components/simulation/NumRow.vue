<template>
  <!-- Numeric override row — uses the global <NumberInput> component
       (no native spinners, custom step buttons, ↑/↓ keyboard nav). -->
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
    <div class="input-wrap">
      <NumberInput
        :model-value="modelValue"
        :min="min"
        :max="max"
        placeholder="—"
        @update:modelValue="$emit('update:modelValue', $event)"
      />
    </div>
  </div>
</template>

<script setup>
import NumberInput from '@/components/NumberInput.vue'

defineProps({
  label: String,
  modelValue: { default: null },
  modified: { type: Boolean, default: false },
  min: { type: Number, default: 1 },
  max: { type: Number, default: 500 },
})
defineEmits(['update:modelValue', 'reset'])
</script>

<style scoped>
.row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  padding: 3px 0;
}
.label {
  flex: 1; min-width: 0;
  font-size: 11px;
  color: var(--color-t2);
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.reset-btn {
  font-size: 10px;
  color: var(--color-t2);
  background: transparent;
  border: none;
  cursor: pointer;
  padding: 0 4px;
  line-height: 1;
}
.reset-btn:hover { color: var(--color-t1); }

.input-wrap {
  flex-shrink: 0;
  width: 88px;
}
</style>
