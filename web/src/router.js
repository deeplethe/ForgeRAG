import { createRouter, createWebHistory } from 'vue-router'
import { getMe } from '@/api/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/chat' },
    { path: '/login', component: () => import('@/views/Login.vue'), meta: { public: true } },
    { path: '/register', component: () => import('@/views/Register.vue'), meta: { public: true } },
    { path: '/setup', component: () => import('@/views/Setup.vue'), meta: { public: true } },
    // Chat route uses an OPTIONAL path param for the conversation id.
    //   /chat            → fresh chat (empty home / new conversation)
    //   /chat/<id>       → resume a specific conversation
    // Keeps query strings free for orthogonal state (cwd, path_filter,
    // etc.); the conv id was a stable resource identifier so it
    // earned the path slot. Deeper folder/doc/chunk state stays in
    // query for the same multi-key flexibility reason.
    { path: '/chat/:id?', component: () => import('@/views/Chat.vue') },
    { path: '/search', component: () => import('@/views/Search.vue') },
    // Library = the indexed knowledge base (formerly "Workspace"); the
    // file manager UI lives at /library. /workspace is now the agent-
    // driven artifact surface — distinct route, distinct view.
    { path: '/library', component: () => import('@/views/Library.vue') },
    { path: '/workspace', component: () => import('@/views/Workspace.vue') },
    { path: '/workspace/:projectId', component: () => import('@/views/ProjectDetail.vue') },
    {
      // Legacy redirect: old /repository?doc=X links land on /library?doc=X.
      // Library embeds DocDetail.vue for the focused-doc view; the
      // original Repository.vue component has been removed.
      path: '/repository',
      redirect: (to) => ({ path: '/library', query: to.query }),
    },
    { path: '/ingestion', redirect: '/library' },
    { path: '/knowledge-graph', component: () => import('@/views/KnowledgeGraph.vue') },
    // Daily-use agent surfaces — capability registry + long /
    // scheduled task queue. Top-level routes (matching the main
    // sidebar entries) rather than tucked into Settings: these
    // are operational, not configuration.
    { path: '/tools', component: () => import('@/views/Tools.vue') },
    { path: '/tasks', component: () => import('@/views/Tasks.vue') },
    // Metrics moved under /settings/metrics so it sits next to the
    // other "look at your account" tools. Keep the legacy
    // top-level URL alive as a redirect — old bookmarks / docs
    // still land somewhere sensible.
    { path: '/metrics', redirect: '/settings/metrics' },
    // Legacy redirect: /tokens (the old "Tokens & Sessions" page) is
    // gone — it split into /settings/sessions (everyone) and
    // /settings/tokens (admin-only). Land both groups on the page
    // they're allowed to see; the admin sub-nav inside Settings
    // surfaces /settings/tokens for the ones who can manage SKs.
    { path: '/tokens', redirect: '/settings/sessions' },
    // ── Settings ─────────────────────────────────────────────────
    // /settings is a shell with its own left sub-nav rendered inside
    // ``Settings.vue``. Sub-routes (account / preferences / users
    // / system / audit) load nested components into Settings.vue's
    // <router-view>. Admin-only sub-routes carry meta.requiresAdmin
    // and are gated client-side AND by route guard (router.beforeEach
    // below) — the same gate the backend enforces on /admin APIs.
    {
      path: '/settings',
      component: () => import('@/views/Settings.vue'),
      redirect: '/settings/profile',
      children: [
        { path: 'profile', component: () => import('@/views/settings/Profile.vue') },
        { path: 'sessions', component: () => import('@/views/settings/Sessions.vue') },
        // Metrics — anyone logged in. Page renders a personal
        // usage card for everyone + an extra admin-only per-user
        // table when the caller has admin role.
        { path: 'metrics', component: () => import('@/views/settings/Metrics.vue') },
        // Legacy redirects — the IA moved these out of Settings
        // into top-level surfaces (/tasks and /tools). Old links
        // still resolve so existing bookmarks don't 404.
        { path: 'scheduled-tasks', redirect: '/tasks' },
        { path: 'plugins', redirect: '/tools' },
        { path: 'team-tools', redirect: '/tools' },
        {
          path: 'tokens',
          component: () => import('@/views/settings/Tokens.vue'),
          meta: { requiresAdmin: true },
        },
        {
          path: 'users',
          component: () => import('@/views/settings/Users.vue'),
          meta: { requiresAdmin: true },
        },
        {
          path: 'audit',
          component: () => import('@/views/settings/Audit.vue'),
          meta: { requiresAdmin: true },
        },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/chat' },
  ],
})

// Admin gate for /settings/* sub-routes flagged with
// ``meta.requiresAdmin``. Mirrors the backend's role check on the
// admin APIs — the UI never renders these links for non-admins
// (Settings.vue filters them out of the sub-nav), but a hand-typed
// URL still needs bouncing. ``getMe`` is cached one tick by the
// browser; we don't keep our own cache to avoid stale-after-promotion
// edge cases (admin demotes a user → the UI should reflect on next
// nav, not after a reload).
let _meCache = null
let _meCacheAt = 0
async function fetchMeCached() {
  const now = Date.now()
  if (_meCache && now - _meCacheAt < 5000) return _meCache
  try {
    _meCache = await getMe()
    _meCacheAt = now
  } catch {
    _meCache = null
    _meCacheAt = now
  }
  return _meCache
}

router.beforeEach(async (to) => {
  if (!to.matched.some((r) => r.meta?.requiresAdmin)) return true
  const me = await fetchMeCached()
  if (!me || me.role !== 'admin') {
    // Non-admin URL access → bounce to the always-visible profile
    // page rather than the route's parent (avoids redirect loops
    // when /settings itself was the typed URL).
    return { path: '/settings/profile', replace: true }
  }
  return true
})

export default router
