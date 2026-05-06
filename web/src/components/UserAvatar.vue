<script setup>
/**
 * UserAvatar — single source of truth for the project's avatar
 * disc. Replaces three earlier inline copies (Profile, Users,
 * UserMenu).
 *
 * Initials rule:
 *   1. If the name contains any CJK character, render the FIRST
 *      one. (Mixed names like "Mr. 张" still render "张" — the
 *      Chinese signal wins.)
 *   2. Else split on whitespace / dot / underscore / dash. If 2+
 *      parts, render the first letter of each of the first two
 *      parts (``"John Doe"`` → ``"JD"``). With one part, render
 *      its first two letters (``"alice"`` → ``"AL"``,
 *      ``"sam"`` → ``"SA"``, single-char ``"x"`` → ``"X"``).
 *   3. Empty / unparseable input falls back to ``"?"``.
 *
 * Email-as-name is supported: the ``@example.com`` suffix is
 * stripped before initials extraction so ``"alice@x.com"`` reads
 * as ``"AL"`` not ``"AL"`` of the local part still works after the
 * strip.
 *
 * Color is a stable HSL hash of the source name string — same
 * ``hash * 31`` rule the previous inline avatars used, just
 * factored out so promote/demote / display-name changes all keep
 * generating the same colour for the same identity.
 *
 * Future: when real avatar images land (URL prop), the disc will
 * fall back to initials only when the URL fails to load. For now
 * everything is initials, so there's no img fallback machinery.
 */
import { computed } from 'vue'

const props = defineProps({
  // The display name to source initials + colour from. Pass the
  // friendly label (display_name | email-prefix | username),
  // not the raw user_id.
  name: { type: String, default: '' },
  // Disc edge length in px. Font-size auto-derives from this so
  // the letters never overflow at unusual sizes.
  size: { type: Number, default: 28 },
  // Default = circle. Square is reserved for embed cases (e.g.
  // a tag chip with avatar inside) where a circle next to other
  // square chips would jar.
  square: { type: Boolean, default: false },
})

const _CJK_RE = /[㐀-䶿一-鿿]/

function _stripEmail(s) {
  const at = s.indexOf('@')
  return at > 0 ? s.slice(0, at) : s
}

const initials = computed(() => {
  const raw = (props.name || '').trim()
  if (!raw) return '?'
  // CJK takes precedence — first matching codepoint wins.
  const m = raw.match(_CJK_RE)
  if (m) return m[0]
  // Strip email suffix BEFORE splitting so domain dots don't
  // become spurious word boundaries (``"a.b@x.com"`` → split on
  // dot → first two parts of ``a.b`` → ``"AB"``, not ``"AX"``).
  const cleaned = _stripEmail(raw).trim()
  if (!cleaned) return '?'
  const parts = cleaned.split(/[\s._-]+/).filter(Boolean)
  if (parts.length >= 2) {
    return (parts[0][0] + parts[1][0]).toUpperCase()
  }
  const only = parts[0] || cleaned
  return only.slice(0, 2).toUpperCase()
})

const bg = computed(() => {
  const k = (props.name || '').trim() || '?'
  let h = 0
  for (let i = 0; i < k.length; i++) h = (h * 31 + k.charCodeAt(i)) >>> 0
  return `hsl(${h % 360}, 55%, 50%)`
})

// Font scales with disc size — ratio 0.45 reads cleanly from 16px
// (badge dots) to 64px (profile cards). Single-char (CJK) sits a
// hair smaller because the glyph fills more of its em-box than
// two latin letters do.
const fontSize = computed(() => {
  const ratio = initials.value.length === 1 ? 0.5 : 0.42
  return `${Math.round(props.size * ratio)}px`
})
</script>

<template>
  <span
    class="avatar"
    :class="{ 'avatar-square': square }"
    :style="{
      width: size + 'px',
      height: size + 'px',
      background: bg,
      fontSize: fontSize,
    }"
    :title="name || ''"
  >{{ initials }}</span>
</template>

<style scoped>
.avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  color: #fff;
  font-weight: 600;
  letter-spacing: 0.01em;
  user-select: none;
  flex-shrink: 0;
  /* Counter-balance the perceived weight of a flat-fill disc on
     dark themes — a hairline inner shadow keeps it from pasting
     onto the background. */
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.06);
}
.avatar-square {
  border-radius: 4px;
}
</style>
