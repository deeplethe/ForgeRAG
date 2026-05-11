<template>
  <!--
    Plugins — placeholder.

    Future home for: enable/disable individual MCP tool packs,
    register custom MCP servers (user-defined integrations: Linear /
    Jira / GitHub / internal APIs), see which tools each plugin
    contributes to the agent's tool list.

    The MCP wiring already exists (api/routes/mcp_server.py
    registers tools at boot); a future iteration moves that to a
    dynamic registry the operator can edit from this page.
  -->
  <div class="page">
    <header class="page-header">
      <h1 class="page-title">{{ t('settings.nav.plugins') }}</h1>
      <p class="page-hint">{{ t('settings.plugins.subtitle') }}</p>
    </header>

    <div class="placeholder">
      <div class="placeholder-icon"><Plug :size="28" :stroke-width="1.25" /></div>
      <h2 class="placeholder-title">{{ t('settings.plugins.coming_soon') }}</h2>
      <p class="placeholder-text">{{ t('settings.plugins.placeholder_desc') }}</p>
    </div>
  </div>
</template>

<script setup>
import { useI18n } from 'vue-i18n'
import { Plug } from 'lucide-vue-next'

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
