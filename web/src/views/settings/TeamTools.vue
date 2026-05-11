<template>
  <!--
    Team tools (工具站) — placeholder.

    The intent: a small registry of team-internal "tool apps" —
    a glossary lookup, a domain-specific Q&A assistant, an
    expense-categorizer, anything the team builds on top of the
    agent that should have its own URL + one-click launcher.
    Each tool is essentially a saved prompt + tool config + chat
    surface, surfaced as a tile here so non-power users can find
    them without writing their own prompt.

    Real implementation: extension of the prompt presets the chat
    composer already loads; needs a small "tool app" table +
    /tools/<slug> route + tile UI.
  -->
  <div class="page">
    <header class="page-header">
      <h1 class="page-title">{{ t('settings.nav.team_tools') }}</h1>
      <p class="page-hint">{{ t('settings.team_tools.subtitle') }}</p>
    </header>

    <div class="placeholder">
      <div class="placeholder-icon"><LayoutGrid :size="28" :stroke-width="1.25" /></div>
      <h2 class="placeholder-title">{{ t('settings.team_tools.coming_soon') }}</h2>
      <p class="placeholder-text">{{ t('settings.team_tools.placeholder_desc') }}</p>
    </div>
  </div>
</template>

<script setup>
import { useI18n } from 'vue-i18n'
import { LayoutGrid } from 'lucide-vue-next'

const { t } = useI18n()
</script>

<style scoped>
.page { padding: 24px 28px; max-width: 880px; }
.page-header { margin-bottom: 24px; }
.page-title { font-size: 1.125rem; font-weight: 600; color: var(--color-t1); margin: 0; }
.page-hint { margin: 6px 0 0; font-size: 0.8125rem; color: var(--color-t2); line-height: 1.55; }
.placeholder {
  margin-top: 24px;
  padding: 48px 24px;
  background: var(--color-bg2);
  border: 1px dashed var(--color-line);
  border-radius: 10px;
  text-align: center;
  color: var(--color-t2);
}
.placeholder-icon { color: var(--color-t3); margin-bottom: 12px; }
.placeholder-title { font-size: 0.875rem; font-weight: 500; color: var(--color-t1); margin: 0 0 6px; }
.placeholder-text { font-size: 0.75rem; color: var(--color-t3); margin: 0; line-height: 1.6; max-width: 480px; margin-inline: auto; }
</style>
