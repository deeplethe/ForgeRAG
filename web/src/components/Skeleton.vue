<template>
  <!--
    Skeleton placeholder bar / block. Vercel pattern: a subtle shimmer
    that lasts only as long as the data is actually missing — no jarring
    spinners, no full-screen blocks.

    Usage:
      <Skeleton :w="180" :h="14" />            inline pill, fixed width
      <Skeleton w="100%" :h="14" />            full-width row
      <Skeleton block :h="80" :rounded="8" />  block placeholder

    The shimmer is a CSS-only animated gradient over `--color-bg3`, so it
    auto-adapts in dark mode. Disable per-instance with `static`.
  -->
  <span
    :class="['skel', { 'skel-block': block, 'skel-static': static }]"
    :style="style"
  ></span>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  w: { type: [String, Number], default: '100%' },
  h: { type: [String, Number], default: 12 },
  rounded: { type: [String, Number], default: 4 },
  block: { type: Boolean, default: false },
  static: { type: Boolean, default: false },
})

function px(v) {
  if (v == null) return undefined
  if (typeof v === 'number') return `${v}px`
  return v
}

const style = computed(() => ({
  width: px(props.w),
  height: px(props.h),
  borderRadius: px(props.rounded),
}))
</script>

<style scoped>
.skel {
  display: inline-block;
  vertical-align: middle;
  background: var(--color-bg3);
  position: relative;
  overflow: hidden;
}
.skel-block { display: block; }

/* Shimmer — translates a faint highlight band across the surface. Uses
   color-mix so the highlight is just slightly lighter than bg3, producing
   a soft pulse that's visible in both light and dark modes. */
.skel:not(.skel-static)::after {
  content: '';
  position: absolute;
  inset: 0;
  background: linear-gradient(
    90deg,
    transparent 0%,
    color-mix(in srgb, var(--color-bg) 70%, transparent) 50%,
    transparent 100%
  );
  transform: translateX(-100%);
  animation: skel-shimmer 1.2s ease-in-out infinite;
}

@keyframes skel-shimmer {
  0%   { transform: translateX(-100%); }
  100% { transform: translateX(100%); }
}

/* Respect prefers-reduced-motion */
@media (prefers-reduced-motion: reduce) {
  .skel::after { animation: none; opacity: 0.4; }
}
</style>
