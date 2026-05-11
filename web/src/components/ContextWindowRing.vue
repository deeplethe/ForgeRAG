<template>
  <!--
    Small donut showing what fraction of the model's context window
    the current conversation occupies. Mirrors Claude.ai's spinner-
    style ring next to the send button — visual cue that "this chat
    is getting long, the model is seeing X / Y tokens on each call".

    Props:
      used  — tokens the model SAW on its last call (= prior history
              + prompt + tool results). Read from the latest
              assistant message's ``input_tokens``.
      limit — model's published context window in tokens. Read from
              /health -> features.generator_context_window.

    The ring renders even at 0% (greyed) so the user discovers it
    naturally; the hover tooltip explains what it means.
  -->
  <div
    class="ctx-ring"
    :class="ringClass"
    :title="tooltip"
  >
    <svg :width="size" :height="size" viewBox="0 0 32 32" aria-hidden="true">
      <!-- Track -->
      <circle
        cx="16" cy="16" :r="radius"
        fill="none"
        :stroke="trackColor"
        :stroke-width="strokeWidth"
      />
      <!-- Progress -->
      <circle
        v-if="ratio > 0"
        cx="16" cy="16" :r="radius"
        fill="none"
        :stroke="progressColor"
        :stroke-width="strokeWidth"
        stroke-linecap="round"
        :stroke-dasharray="circumference"
        :stroke-dashoffset="dashOffset"
        transform="rotate(-90 16 16)"
        class="ctx-ring__progress"
      />
    </svg>
    <span v-if="showLabel" class="ctx-ring__pct">{{ pctLabel }}</span>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  used: { type: Number, default: 0 },
  limit: { type: Number, default: 0 },
  size: { type: Number, default: 16 },
  strokeWidth: { type: Number, default: 3 },
  showLabel: { type: Boolean, default: false },
})

const radius = computed(() => 16 - props.strokeWidth / 2 - 0.5)
const circumference = computed(() => 2 * Math.PI * radius.value)
const ratio = computed(() => {
  if (!props.limit || props.limit <= 0) return 0
  if (!props.used || props.used <= 0) return 0
  return Math.min(1, props.used / props.limit)
})
const dashOffset = computed(() => circumference.value * (1 - ratio.value))
const pct = computed(() => Math.round(ratio.value * 100))
const pctLabel = computed(() => `${pct.value}%`)

// Color rises with usage — brand-blue under 80%, amber up to 95%,
// red beyond. Track stays neutral.
const progressColor = computed(() => {
  const r = ratio.value
  if (r >= 0.95) return 'var(--color-err-fg)'
  if (r >= 0.80) return 'var(--color-amber)'
  return 'var(--color-brand)'
})
const trackColor = 'var(--color-line)'

const ringClass = computed(() => ({
  'ctx-ring--warn': ratio.value >= 0.80 && ratio.value < 0.95,
  'ctx-ring--full': ratio.value >= 0.95,
}))

function fmtTokens(n) {
  if (!n || n <= 0) return '0'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`
  if (n >= 1000) return `${(n / 1000).toFixed(1).replace(/\.0$/, '')}k`
  return String(n)
}

const tooltip = computed(() => {
  if (!props.limit) return 'Context window'
  return `${fmtTokens(props.used)} / ${fmtTokens(props.limit)} (${pct.value}%)`
})
</script>

<style scoped>
.ctx-ring {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  cursor: default;
  /* Subtle hover lift — paired with the tooltip so the user knows
     it's a "passive indicator with detail on hover", not a button. */
  transition: opacity .15s;
}
.ctx-ring:hover { opacity: 0.85; }
.ctx-ring__progress {
  transition: stroke-dashoffset .25s ease, stroke .15s;
}
.ctx-ring__pct {
  font-size: 0.625rem;
  color: var(--color-t3);
  font-feature-settings: "tnum";
  letter-spacing: -0.01em;
}
.ctx-ring--warn .ctx-ring__pct { color: var(--color-amber); }
.ctx-ring--full .ctx-ring__pct { color: var(--color-err-fg); }
</style>
