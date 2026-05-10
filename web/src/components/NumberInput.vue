<template>
  <!--
    Reusable numeric input with no native chrome.

    Why not <input type="number">?
      - Native spinner buttons look browser-default & inconsistent across OS
      - Mouse-wheel mutates the value while scrolling the page (UX trap)
      - Number-localization quirks (1,234 vs 1.234)
      - Can't fully restyle the spinner

    This component:
      - text input + inputmode="numeric" (mobile shows numeric keypad)
      - Custom integer-only filtering on input
      - Up/Down arrow keys to step (configurable :step)
      - Optional ▴/▾ steppers on hover
      - Empty input = null (preserves "use yaml default" semantics)
      - Min/max clamping on commit (blur), not on each keystroke

    Usage:
      <NumberInput v-model="topK" :min="1" :max="500" placeholder="—" />
      <NumberInput v-model="rps" :min="0.1" :max="100" :step="0.1" />
  -->
  <div class="ni-root" :class="{ 'ni-disabled': disabled, 'ni-focus': focused }">
    <input
      ref="inp"
      type="text"
      inputmode="numeric"
      autocomplete="off"
      :value="display"
      :placeholder="placeholder"
      :disabled="disabled"
      :aria-valuenow="modelValue"
      :aria-valuemin="min"
      :aria-valuemax="max"
      role="spinbutton"
      @input="onInput"
      @focus="focused = true"
      @blur="onBlur"
      @keydown.up.prevent="step(+1)"
      @keydown.down.prevent="step(-1)"
      class="ni-input"
    />
    <div v-if="showSteppers" class="ni-steppers" aria-hidden="true">
      <button
        type="button"
        class="ni-step"
        @mousedown.prevent
        @click="step(+1)"
        :disabled="disabled || !canInc"
        tabindex="-1"
      >▴</button>
      <button
        type="button"
        class="ni-step"
        @mousedown.prevent
        @click="step(-1)"
        :disabled="disabled || !canDec"
        tabindex="-1"
      >▾</button>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  modelValue: { type: [Number, String, null], default: null },
  min: { type: Number, default: -Infinity },
  max: { type: Number, default: Infinity },
  step: { type: Number, default: 1 },
  placeholder: { type: String, default: '' },
  disabled: { type: Boolean, default: false },
  showSteppers: { type: Boolean, default: true },
  /** When true, the input value is allowed to be a float; otherwise rounds to int. */
  float: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue', 'change'])

const inp = ref(null)
const focused = ref(false)

// What's currently typed in the box. Tracked separately from modelValue so
// the user can clear the field (typing nothing) without us re-injecting "0".
const typed = ref(stringify(props.modelValue))

// When the bound value changes externally (preset load, reset, etc.), sync.
import { watch } from 'vue'
watch(() => props.modelValue, (v) => { typed.value = stringify(v) })

const display = computed(() => focused.value ? typed.value : stringify(props.modelValue))

const canInc = computed(() => props.modelValue == null || (+props.modelValue + props.step) <= props.max)
const canDec = computed(() => props.modelValue == null || (+props.modelValue - props.step) >= props.min)

function stringify(v) {
  if (v == null || v === '') return ''
  return String(v)
}

function parse(raw) {
  if (raw == null) return null
  const trimmed = String(raw).trim()
  if (!trimmed) return null
  // Allow a leading "-" or a single "."  for in-progress typing
  if (trimmed === '-' || trimmed === '.') return undefined
  const n = props.float ? parseFloat(trimmed) : parseInt(trimmed, 10)
  if (Number.isNaN(n)) return undefined
  return n
}

function clamp(n) {
  if (n == null || isNaN(n)) return null
  return Math.min(props.max, Math.max(props.min, n))
}

function onInput(e) {
  // Allow only digits, optional leading "-", and (if float) a single "."
  const re = props.float ? /[^0-9.\-]/g : /[^0-9\-]/g
  const cleaned = e.target.value.replace(re, '')
  // Only one leading "-" + only one "."
  const final = cleaned
    .replace(/(?!^)-/g, '')
    .replace(/(\..*)\./g, '$1')
  if (final !== e.target.value) e.target.value = final
  typed.value = final
  const parsed = parse(final)
  if (parsed === undefined) return  // mid-typing, don't emit
  // Don't clamp during typing; commit-time clamp on blur. Just emit raw.
  if (parsed === null) {
    emit('update:modelValue', null)
  } else {
    emit('update:modelValue', parsed)
  }
}

function onBlur() {
  focused.value = false
  const parsed = parse(typed.value)
  if (parsed === undefined) {
    // Mid-typed garbage — restore to last valid model value
    typed.value = stringify(props.modelValue)
    return
  }
  if (parsed === null) {
    emit('update:modelValue', null)
    emit('change', null)
    return
  }
  const clamped = clamp(parsed)
  typed.value = stringify(clamped)
  if (clamped !== props.modelValue) {
    emit('update:modelValue', clamped)
    emit('change', clamped)
  }
}

function step(direction) {
  const base = props.modelValue == null ? 0 : +props.modelValue
  const next = clamp(base + direction * props.step)
  emit('update:modelValue', next)
  emit('change', next)
  typed.value = stringify(next)
}
</script>

<style scoped>
.ni-root {
  position: relative;
  display: inline-flex;
  align-items: stretch;
  width: 100%;
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: var(--r-sm);
  transition: border-color 0.12s, box-shadow 0.12s;
}
.ni-root:hover { border-color: var(--color-line2); }
.ni-root.ni-focus { border-color: var(--color-line2); box-shadow: var(--ring-focus); }
.ni-root.ni-disabled { opacity: 0.5; pointer-events: none; }

.ni-input {
  flex: 1;
  min-width: 0;
  padding: 4px 8px;
  font-size: 0.6875rem;
  color: var(--color-t1);
  background: transparent;
  border: none;
  outline: none;
  font-variant-numeric: tabular-nums;
  text-align: right;
  /* No native spinners */
  -moz-appearance: textfield;
  appearance: textfield;
}
.ni-input::-webkit-inner-spin-button,
.ni-input::-webkit-outer-spin-button { -webkit-appearance: none; margin: 0; }
.ni-input::placeholder { color: var(--color-t3); }

.ni-steppers {
  display: flex;
  flex-direction: column;
  border-left: 1px solid var(--color-line);
  opacity: 0;
  transition: opacity 0.12s;
}
.ni-root:hover .ni-steppers,
.ni-root.ni-focus .ni-steppers { opacity: 1; }

.ni-step {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  font-size: 0.5rem;
  color: var(--color-t3);
  background: transparent;
  border: none;
  cursor: pointer;
  padding: 0;
  line-height: 1;
}
.ni-step:not(:last-child) { border-bottom: 1px solid var(--color-line); }
.ni-step:hover:not(:disabled) { background: var(--color-bg3); color: var(--color-t1); }
.ni-step:disabled { opacity: 0.3; cursor: not-allowed; }
</style>
