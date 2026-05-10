/**
 * Cross-view chat-stream state.
 *
 * Singleton reactive ref so the sidebar's per-conversation row can
 * show a streaming spinner while ``Chat.vue`` is mid-turn —
 * regardless of whether the chat view is currently mounted (KeepAlive
 * keeps it cached, but the sidebar lives at App.vue scope and can't
 * reach into ``Chat.vue``'s module-private refs without a shared
 * surface like this one).
 *
 * The chat view writes ``streamingConvId.value`` whenever a stream
 * starts/ends. Every other reader treats it as read-only state.
 */
import { ref } from 'vue'

const _streamingConvId = ref(null)

export function useChatStreamState() {
  return {
    streamingConvId: _streamingConvId,
    setStreamingConv(convId) {
      _streamingConvId.value = convId || null
    },
  }
}
