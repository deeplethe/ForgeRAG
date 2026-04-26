<template>
  <!--
    iOS-style toggle switch, Vercel-density. Vercel pattern:
      - off: subtle gray track + white knob (bg-bg3)
      - on:  near-black track + white knob (bg-t1) — auto-inverts in dark mode

    Used for boolean parameters across the app (replaces the old segmented
    on/off control). Width/height ~28×16, knob 12, slides 12px.
  -->
  <button
    type="button"
    role="switch"
    :aria-checked="!!modelValue"
    :disabled="disabled"
    class="toggle"
    :class="{ on: modelValue }"
    @click="onClick"
  >
    <span class="knob"></span>
  </button>
</template>

<script setup>
const props = defineProps({
  modelValue: { type: Boolean, default: false },
  disabled: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue', 'change'])

function onClick() {
  if (props.disabled) return
  const next = !props.modelValue
  emit('update:modelValue', next)
  emit('change', next)
}
</script>

<style scoped>
.toggle {
  position: relative;
  display: inline-block;
  width: 28px;
  height: 16px;
  padding: 0;
  border-radius: 999px;
  border: none;
  background: var(--color-bg3);
  cursor: pointer;
  vertical-align: middle;
  transition: background 0.15s ease;
  flex-shrink: 0;
}
.toggle:hover:not(:disabled) { background: var(--color-line2); }
.toggle.on { background: var(--color-t1); }
.toggle.on:hover:not(:disabled) { background: var(--color-t1-hover); }
.toggle:focus-visible { outline: none; box-shadow: var(--ring-focus); }
.toggle:disabled { opacity: 0.45; cursor: not-allowed; }

.knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--color-bg);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.15);
  transition: transform 0.15s ease, background 0.15s ease;
}
.toggle.on .knob { transform: translateX(12px); }
</style>
