<script setup>
/**
 * UserAvatar — single source of truth for the project's avatar
 * disc. Used by Profile, UserMenu, Users, Search, and anywhere
 * else a user identity needs a visual.
 *
 * Two display modes:
 *
 *   1. ``imgUrl`` provided AND it loads → render the uploaded
 *      avatar image (cover-fitted, never distorted).
 *   2. No URL, OR the image errors / 404s → render colored disc
 *      with initials.
 *
 * The fallback is automatic: an ``onerror`` flips a local
 * ``imgFailed`` flag, swapping the <img> for the initials disc.
 * Callers can pass ``imgUrl`` unconditionally without checking
 * has_avatar themselves — a 404 just degrades gracefully. (The
 * ``has-avatar`` flag from /me is used elsewhere as a hint to
 * avoid the round-trip on users with no avatar; this component
 * stays robust either way.)
 *
 * Initials rule (used in fallback):
 *   1. CJK present anywhere in name → first matching codepoint
 *      ("张三" → "张", "Mr. 王" → "王").
 *   2. Else split on whitespace / dot / underscore / dash. 2+
 *      parts → first letter of each of the first two
 *      ("John Doe" → "JD"). 1 part → first 2 letters
 *      ("alice" → "AL"). Single char → just it ("X" → "X").
 *   3. Empty / unparseable → "?".
 *
 * Email-as-name is supported: the ``@example.com`` suffix is
 * stripped before initials extraction.
 *
 * Color is a stable HSL hash of the source name string. Same
 * identity always gets the same colour across the app.
 */
import { computed, ref, watch } from 'vue'

const props = defineProps({
  // Display name to source initials + colour from. Pass the
  // friendly label (display_name | email-prefix | username),
  // not the raw user_id.
  name: { type: String, default: '' },
  // Optional uploaded-avatar URL. When the image loads
  // successfully it's shown over the colored disc; on error /
  // missing the disc + initials remain.
  imgUrl: { type: String, default: '' },
  // Disc edge length in px. Font-size auto-derives.
  size: { type: Number, default: 28 },
  // Default = circle. Square reserved for embed cases.
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
  const m = raw.match(_CJK_RE)
  if (m) return m[0]
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

const fontSize = computed(() => {
  const ratio = initials.value.length === 1 ? 0.5 : 0.42
  return `${Math.round(props.size * ratio)}px`
})

// ── Image loading state ───────────────────────────────────
// imgFailed flips on the first <img> error and pins the
// fallback. Reset when the URL changes (e.g. after a fresh
// upload — the no-cache header on the GET handler means the
// browser refetches).
const imgFailed = ref(false)
watch(() => props.imgUrl, () => { imgFailed.value = false })

const showImg = computed(() => !!props.imgUrl && !imgFailed.value)
</script>

<template>
  <span
    class="avatar"
    :class="{ 'avatar-square': square, 'avatar-with-img': showImg }"
    :style="{
      width: size + 'px',
      height: size + 'px',
      background: bg,
      fontSize: fontSize,
    }"
    :title="name || ''"
  >
    <img
      v-if="showImg"
      :src="imgUrl"
      :alt="name || ''"
      class="avatar-img"
      @error="imgFailed = true"
    />
    <template v-else>{{ initials }}</template>
  </span>
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
  overflow: hidden;
  position: relative;
  /* Counter-balance the perceived weight of a flat-fill disc on
     dark themes — a hairline inner shadow keeps it from pasting
     onto the background. */
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.06);
}
.avatar-square {
  border-radius: 4px;
}
/* When the <img> renders we don't want the bg colour to peek
   through transparent PNGs — set bg to transparent so the image
   carries the entire visual. The hash colour stays as a
   "loading" tint for one paint, which is fine. */
.avatar-with-img {
  background: transparent !important;
}
.avatar-img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
</style>
