/**
 * Theme (light / dark) — singleton state shared across the app.
 *
 * Storage shape (localStorage key `forgerag.theme.v1`):
 *   "light" | "dark"
 *
 * Default = system preference (`prefers-color-scheme`) on first visit;
 * once the user toggles, their explicit choice wins forever.
 *
 * The current theme is reflected on `<html data-theme="...">`. Style.css
 * branches via `[data-theme="dark"] { ... }` overrides.
 *
 * To avoid FOUC on initial page load, an inline script in index.html applies
 * the stored theme synchronously before Vue mounts. This composable simply
 * keeps the reactive copy in sync afterwards.
 */

import { computed, ref } from 'vue'

const LS_KEY = 'opencraig.theme.v1'

function readInitial() {
  if (typeof localStorage !== 'undefined') {
    const v = localStorage.getItem(LS_KEY)
    if (v === 'dark' || v === 'light') return v
  }
  if (typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: dark)').matches) {
    return 'dark'
  }
  return 'light'
}

const _theme = ref(readInitial())

function applyToDOM(t) {
  if (typeof document === 'undefined') return
  document.documentElement.dataset.theme = t
}
applyToDOM(_theme.value)

export function useTheme() {
  const theme = computed({
    get: () => _theme.value,
    set: (v) => setTheme(v),
  })
  const isDark = computed(() => _theme.value === 'dark')

  return { theme, isDark, setTheme, toggleTheme }
}

export function setTheme(t) {
  if (t !== 'light' && t !== 'dark') return
  _theme.value = t
  applyToDOM(t)
  try { localStorage.setItem(LS_KEY, t) } catch {}
}

export function toggleTheme() {
  setTheme(_theme.value === 'dark' ? 'light' : 'dark')
}
