<template>
  <!--
    Vercel-style date picker. Self-contained, dependency-free.

    Usage:
      <DatePicker v-model="iso"
                  placeholder="Select date"
                  :min-date="new Date()"
                  show-shortcuts />

    Model value: ISO date string "YYYY-MM-DD" (or '' / null for empty).

    Props:
      modelValue   string | null
      placeholder  string         placeholder text in trigger when empty
      minDate      Date | string  earliest selectable (inclusive)
      maxDate      Date | string  latest selectable (inclusive)
      showShortcuts boolean       show "+7d / +30d / +90d / 1y" quick rows
      clearable    boolean        show clear button when a value is set (default true)
  -->
  <div class="datepicker" ref="rootEl">
    <button
      type="button"
      class="trigger"
      :class="{ active: open }"
      @click="toggle"
    >
      <span v-if="modelValue" class="value">{{ display(modelValue) }}</span>
      <span v-else class="placeholder">{{ placeholder || 'Select date' }}</span>
      <svg class="cal-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
        <rect x="3" y="5" width="18" height="16" rx="2"/>
        <path d="M3 9h18M8 3v4M16 3v4"/>
      </svg>
    </button>

    <Teleport to="body">
      <div
        v-if="open"
        class="dp-popover panel"
        :style="popStyle"
        @click.stop
      >
        <header class="dp-head">
          <button class="btn-icon" @click="prevMonth" title="Previous month">‹</button>
          <span class="dp-title">{{ monthTitle }}</span>
          <button class="btn-icon" @click="nextMonth" title="Next month">›</button>
        </header>

        <div class="dp-grid">
          <span v-for="d in weekdays" :key="d" class="dow">{{ d }}</span>
          <button
            v-for="d in dayCells"
            :key="d.iso"
            type="button"
            class="day"
            :class="{
              'day-out': !d.inMonth,
              'day-today': d.today,
              'day-active': d.iso === modelValue,
              'day-disabled': d.disabled,
            }"
            :disabled="d.disabled"
            @click="pick(d)"
          >{{ d.n }}</button>
        </div>

        <div v-if="showShortcuts" class="dp-shortcuts">
          <button v-for="s in shortcuts" :key="s.label"
            type="button" class="shortcut" @click="pickOffset(s.days)">
            {{ s.label }}
          </button>
        </div>

        <footer class="dp-foot">
          <button type="button" class="link" @click="pickOffset(0)">Today</button>
          <button v-if="clearable && modelValue" type="button" class="link link-muted" @click="clear">
            Clear
          </button>
        </footer>
      </div>
    </Teleport>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

const props = defineProps({
  modelValue: { type: [String, null], default: '' },
  placeholder: { type: String, default: '' },
  minDate: { type: [Date, String, null], default: null },
  maxDate: { type: [Date, String, null], default: null },
  showShortcuts: { type: Boolean, default: false },
  clearable: { type: Boolean, default: true },
})
const emit = defineEmits(['update:modelValue', 'change'])

const open = ref(false)
const rootEl = ref(null)
const popStyle = ref({})

// View state — month being displayed in the calendar
const today = new Date()
const viewYear = ref(today.getFullYear())
const viewMonth = ref(today.getMonth())

// When opening, jump the view to the selected date's month
watch(open, (v) => {
  if (!v) return
  const sel = props.modelValue
  if (sel) {
    const d = new Date(sel)
    if (!isNaN(d)) {
      viewYear.value = d.getFullYear()
      viewMonth.value = d.getMonth()
    }
  }
})

// ── Helpers ───────────────────────────────────────────────────────
function pad2(n) { return n < 10 ? '0' + n : '' + n }
function isoOf(d) { return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) }
function todayIso() { return isoOf(new Date()) }
function asDate(v) {
  if (!v) return null
  if (v instanceof Date) return v
  const d = new Date(v)
  return isNaN(d) ? null : d
}

function display(iso) {
  const d = new Date(iso)
  if (isNaN(d)) return iso
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

const weekdays = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

const monthTitle = computed(() => {
  const d = new Date(viewYear.value, viewMonth.value, 1)
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'long' })
})

const minD = computed(() => asDate(props.minDate))
const maxD = computed(() => asDate(props.maxDate))

const dayCells = computed(() => {
  // Build the 6×7 grid: include trailing days of prev month + leading days of next.
  const y = viewYear.value
  const m = viewMonth.value
  const first = new Date(y, m, 1)
  const dowFirst = first.getDay()
  const startDate = new Date(y, m, 1 - dowFirst)
  const todayKey = todayIso()
  const cells = []
  for (let i = 0; i < 42; i++) {
    const d = new Date(startDate.getFullYear(), startDate.getMonth(), startDate.getDate() + i)
    const iso = isoOf(d)
    const inMonth = d.getMonth() === m
    let disabled = false
    if (minD.value && d < stripTime(minD.value)) disabled = true
    if (maxD.value && d > stripTime(maxD.value)) disabled = true
    cells.push({
      n: d.getDate(),
      iso,
      inMonth,
      today: iso === todayKey,
      disabled,
    })
  }
  return cells
})

function stripTime(d) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate())
}

const shortcuts = [
  { label: '+7d',  days: 7 },
  { label: '+30d', days: 30 },
  { label: '+90d', days: 90 },
  { label: '1 year', days: 365 },
]

// ── Actions ───────────────────────────────────────────────────────
function toggle() {
  if (open.value) { close(); return }
  positionPop()
  open.value = true
}

function close() { open.value = false }

function prevMonth() {
  if (viewMonth.value === 0) { viewYear.value--; viewMonth.value = 11 }
  else viewMonth.value--
}
function nextMonth() {
  if (viewMonth.value === 11) { viewYear.value++; viewMonth.value = 0 }
  else viewMonth.value++
}

function pick(d) {
  if (d.disabled) return
  emit('update:modelValue', d.iso)
  emit('change', d.iso)
  close()
}

function pickOffset(days) {
  const d = new Date()
  d.setDate(d.getDate() + days)
  const iso = isoOf(d)
  emit('update:modelValue', iso)
  emit('change', iso)
  close()
}

function clear() {
  emit('update:modelValue', '')
  emit('change', '')
  close()
}

// Position the popover below the trigger. Recomputed on open + on scroll
// for safety. Uses fixed positioning so it works inside scrolled containers
// and modals (Teleport to body).
function positionPop() {
  const el = rootEl.value
  if (!el) return
  const r = el.getBoundingClientRect()
  const popW = 280  // matches CSS width
  // Prefer below; flip above if not enough room
  const spaceBelow = window.innerHeight - r.bottom
  const popH = 340  // approx
  const goesUp = spaceBelow < popH && r.top > popH
  popStyle.value = {
    position: 'fixed',
    top: goesUp ? `${r.top - popH - 4}px` : `${r.bottom + 4}px`,
    left: `${Math.max(8, Math.min(window.innerWidth - popW - 8, r.left))}px`,
  }
}

// ── Outside click + Esc to close ─────────────────────────────────
function onDocClick(e) {
  if (!open.value) return
  if (rootEl.value?.contains(e.target)) return
  // Anything outside trigger AND outside popover closes
  const pop = document.querySelector('.dp-popover')
  if (pop?.contains(e.target)) return
  close()
}
function onKey(e) { if (open.value && e.key === 'Escape') close() }
function onResize() { if (open.value) positionPop() }

onMounted(() => {
  document.addEventListener('mousedown', onDocClick)
  document.addEventListener('keydown', onKey)
  window.addEventListener('resize', onResize)
  window.addEventListener('scroll', onResize, true)
})
onUnmounted(() => {
  document.removeEventListener('mousedown', onDocClick)
  document.removeEventListener('keydown', onKey)
  window.removeEventListener('resize', onResize)
  window.removeEventListener('scroll', onResize, true)
})
</script>

<style scoped>
.datepicker { position: relative; display: block; }

.trigger {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 6px 10px;
  font-size: 11px;
  color: var(--color-t1);
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: var(--r-sm);
  cursor: pointer;
  transition: border-color 0.12s, box-shadow 0.12s;
  text-align: left;
}
.trigger:hover { border-color: var(--color-line2); }
.trigger.active { border-color: var(--color-line2); box-shadow: var(--ring-focus); }
.placeholder { color: var(--color-t3); }
.cal-icon { width: 14px; height: 14px; color: var(--color-t3); flex-shrink: 0; margin-left: 8px; }

/* ── Popover ──────────────────────────────────────────────────── */
.dp-popover {
  width: 280px;
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: var(--r-md);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.10);
  z-index: 90;
  padding: 12px;
  font-size: 11px;
  color: var(--color-t1);
}

/* ── Header ───────────────────────────────────────────────────── */
.dp-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
.dp-title {
  font-size: 12px;
  font-weight: 500;
  color: var(--color-t1);
  letter-spacing: -0.01em;
}

/* ── Grid ─────────────────────────────────────────────────────── */
.dp-grid {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 2px;
}
.dow {
  font-size: 9px;
  font-weight: 500;
  color: var(--color-t3);
  text-align: center;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 6px 0 4px;
}
.day {
  font-size: 11px;
  color: var(--color-t1);
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--r-sm);
  padding: 6px 0;
  cursor: pointer;
  font-variant-numeric: tabular-nums;
  text-align: center;
  transition: background 0.1s, border-color 0.1s, color 0.1s;
}
.day:hover:not(.day-disabled):not(.day-active) { background: var(--color-bg3); }
.day-out { color: var(--color-t3); opacity: 0.6; }
.day-today { border-color: var(--color-line2); }
.day-active {
  background: var(--color-t1);
  color: var(--color-bg);
  font-weight: 500;
}
.day-disabled { opacity: 0.3; cursor: not-allowed; }

/* ── Shortcuts ────────────────────────────────────────────────── */
.dp-shortcuts {
  display: flex;
  gap: 6px;
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--color-line);
  flex-wrap: wrap;
}
.shortcut {
  padding: 4px 8px;
  font-size: 10px;
  color: var(--color-t2);
  background: var(--color-bg2);
  border: 1px solid var(--color-line);
  border-radius: var(--r-sm);
  cursor: pointer;
  transition: background 0.1s, color 0.1s;
}
.shortcut:hover { background: var(--color-bg3); color: var(--color-t1); border-color: var(--color-line2); }

/* ── Footer ───────────────────────────────────────────────────── */
.dp-foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--color-line);
}
.link {
  font-size: 11px;
  color: var(--color-t2);
  background: transparent;
  border: none;
  cursor: pointer;
  padding: 2px 4px;
}
.link:hover { color: var(--color-t1); }
.link-muted { color: var(--color-t3); }
.link-muted:hover { color: var(--color-err-fg); }

.btn-icon {
  width: 22px;
  height: 22px;
  font-size: 14px;
  line-height: 1;
}
</style>
