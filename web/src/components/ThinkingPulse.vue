<script setup>
/**
 * ThinkingPulse — a calmer "working" indicator than a spinner.
 *
 * Visual: a tiny solid dot in the centre with two concentric
 * rings expanding outward and fading. The rings are phase-
 * offset so there's always one ring mid-pulse, giving a
 * continuous "ripple" feel without ever being busy.
 *
 * Used wherever the app would otherwise reach for a
 * <Loader2 class="animate-spin" />:
 *   * Inline next to "Thinking…" / "Reading passage" / "Tool
 *     running" labels.
 *   * Bottom of the in-flight assistant message.
 *
 * Sizing: ``size`` is the bounding box edge in px. The rings
 * scale to 2.5× so the visible motion fits inside ``size``.
 * The dot is ``size * 0.28`` so it stays proportional from
 * 12px (chip-inline) up to 24px (page-blocking).
 *
 * Color: inherits ``currentColor`` from the surrounding text.
 * Pass ``color`` if you want a one-off override (e.g. amber
 * during a tool call).
 */
import { computed } from 'vue'

const props = defineProps({
  // Bounding box edge in px. The dot + rings auto-scale.
  size: { type: Number, default: 14 },
  // Optional color override; defaults to currentColor.
  color: { type: String, default: '' },
})

const dotSize = computed(() => Math.max(3, Math.round(props.size * 0.28)))
const swatchColor = computed(() => props.color || 'currentColor')
</script>

<template>
  <span
    class="pulse"
    :style="{
      width: size + 'px',
      height: size + 'px',
      color: swatchColor,
    }"
    aria-hidden="true"
  >
    <!-- Two phase-offset rings. Each scales from dot-size up
         to ``size`` and fades to 0 alpha. Border-color uses
         currentColor so the ring tracks any text-color around
         it. -->
    <span class="pulse-ring" :style="{ width: dotSize + 'px', height: dotSize + 'px' }" />
    <span class="pulse-ring pulse-ring-late" :style="{ width: dotSize + 'px', height: dotSize + 'px' }" />
    <!-- Centre dot, filled. Stays a constant size while the
         rings ripple outward. -->
    <span class="pulse-dot" :style="{ width: dotSize + 'px', height: dotSize + 'px' }" />
  </span>
</template>

<style scoped>
.pulse {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  vertical-align: middle;
}

.pulse-dot,
.pulse-ring {
  position: absolute;
  top: 50%;
  left: 50%;
  border-radius: 50%;
  /* Centre on (50%, 50%) — translate by -half-self so the
     ring starts EXACTLY at dot size and grows outward
     symmetrically. */
  transform: translate(-50%, -50%);
}
.pulse-dot {
  background: currentColor;
}
.pulse-ring {
  border: 1.5px solid currentColor;
  background: transparent;
  /* Rings expand from 1× (dot size) to ~3.4× — well past the
     bounding box. ``transform`` doesn't affect layout, so the
     overshoot just paints over neighbouring whitespace and
     gives the indicator real visual presence. Earlier 2.4×
     read as "tiny" especially against a dim t3 colour. */
  animation: pulse-ripple 1.6s linear infinite;
  opacity: 0;
}
/* Second ring, half a period later, so the indicator never
   "rests" — there's always one ring mid-flight. */
.pulse-ring-late {
  animation-delay: 0.8s;
}

@keyframes pulse-ripple {
  0% {
    transform: translate(-50%, -50%) scale(1);
    opacity: 0.85;
  }
  100% {
    transform: translate(-50%, -50%) scale(3.4);
    opacity: 0;
  }
}
</style>
