/**
 * vue-i18n setup with localStorage persistence.
 *
 * Storage shape (key ``forgerag.locale.v1``):
 *   "en" | "zh"
 *
 * Default = browser language match (zh* → zh, anything else → en) on
 * first visit; once the user picks via the UserMenu, their explicit
 * choice wins forever. ``setLocale`` also flips ``<html lang="...">``
 * so the browser does the right thing for hyphenation / TTS / etc.
 *
 * Usage in components:
 *   <script setup>
 *     import { useI18n } from 'vue-i18n'
 *     const { t } = useI18n()
 *   </script>
 *   <template>{{ t('common.ok') }}</template>
 */

import { createI18n } from 'vue-i18n'
import en from '../locales/en.json'
import zh from '../locales/zh.json'

const LS_KEY = 'opencraig.locale.v1'
export const SUPPORTED_LOCALES = [
  { code: 'en', label: 'English' },
  { code: 'zh', label: '中文' },
]

function detectInitial() {
  if (typeof localStorage !== 'undefined') {
    const v = localStorage.getItem(LS_KEY)
    if (SUPPORTED_LOCALES.some(l => l.code === v)) return v
  }
  if (typeof navigator !== 'undefined') {
    const lang = (navigator.language || 'en').toLowerCase()
    if (lang.startsWith('zh')) return 'zh'
  }
  return 'en'
}

export const i18n = createI18n({
  legacy: false,           // Composition API mode
  locale: detectInitial(),
  fallbackLocale: 'en',
  messages: { en, zh },
  // Suppress missing-key warnings in production; in dev they help catch
  // typos but flood the console once we miss-translate a single string.
  missingWarn: import.meta.env.DEV,
  fallbackWarn: import.meta.env.DEV,
})

// Apply lang attribute on initial load too
if (typeof document !== 'undefined') {
  document.documentElement.setAttribute('lang', i18n.global.locale.value)
}

export function setLocale(loc) {
  if (!SUPPORTED_LOCALES.some(l => l.code === loc)) return
  i18n.global.locale.value = loc
  if (typeof localStorage !== 'undefined') localStorage.setItem(LS_KEY, loc)
  if (typeof document !== 'undefined') document.documentElement.setAttribute('lang', loc)
}

export function getLocale() {
  return i18n.global.locale.value
}
