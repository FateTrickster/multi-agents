<template>
  <div class="flex h-full flex-col border border-gray-200 bg-white">
    <div class="flex items-center justify-between border-b border-gray-100 p-3">
      <p class="text-xs font-semibold uppercase tracking-wide text-gray-400">小组实时讨论</p>
      <span class="text-xs text-gray-500">{{ onlineCount }} 人在线</span>
    </div>

    <div v-if="!connected" class="bg-yellow-100 py-1 text-center text-xs text-yellow-700">
      Reconnecting to chat server...
    </div>

    <MessageList
      ref="messageListRef"
      :messages="chatStore.messages"
      :has-more="chatStore.hasMore"
      class="flex-1"
      @load-more="handleLoadMore"
      @viewport-change="handleViewportChange"
    />

    <AgentTypingIndicator />

    <div v-if="invokeStatusList.length > 0" class="space-y-1 px-3 pb-2">
      <div
        v-for="item in invokeStatusList"
        :key="item.key"
        class="rounded-lg border px-2 py-1 text-xs"
        :class="statusClass(item.status)"
      >
        {{ roleLabel(item.agent_role) }}: {{ statusText(item.status, item.message) }}
      </div>
    </div>

    <div v-if="unreadCount > 0" class="px-3 pb-2">
      <button
        class="w-full rounded-lg border border-blue-100 bg-blue-50 py-2 text-xs text-blue-600 transition-colors hover:bg-blue-100"
        @click="scrollToLatest"
      >
        {{ unreadCount }} new messages, click to jump to latest
      </button>
    </div>

    <ChatInput
      :agent-mode="currentAgentMode"
      :agent-busy="agentBusy"
      :cooling-roles="coolingRoles"
      :cooldown-until-by-role="cooldownUntilByRole"
      class="shrink-0"
      @send="handleSendMessage"
    />
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import ChatInput from './ChatInput.vue'
import MessageList from './MessageList.vue'
import AgentTypingIndicator from './AgentTypingIndicator.vue'
import { useAgentStream } from '../../composables/useAgentStream.js'
import { useWebSocket } from '../../composables/useWebSocket.js'
import { useAgentStore } from '../../stores/agent.js'
import { useAuthStore } from '../../stores/auth.js'
import { useChatStore } from '../../stores/chat.js'
import { useRoomStore } from '../../stores/room.js'

const route = useRoute()
const authStore = useAuthStore()
const chatStore = useChatStore()
const roomStore = useRoomStore()
const agentStore = useAgentStore()
const roomId = String(route.params.id || '')

const messageListRef = ref(null)
const onlineCount = ref(0)
const unreadCount = ref(0)
const isAtBottom = ref(true)
const invokeStatusMap = ref(new Map())
const cooldownUntilByRole = ref({})

const { connect, on, send, disconnect, connected } = useWebSocket(roomId)
const { handleTyping, handleStream, handleStreamEnd } = useAgentStream()

const ROLE_NAMES = {
  socratic: '单智能体助手',
  facilitator: '主持人',
  devil_advocate: '批判者',
  summarizer: '总结者',
  resource_finder: '资源检索者',
  encourager: '鼓励者',
  concept_explainer: '概念解释员',
}
const currentAgentMode = computed(() => roomStore.currentRoom?.agent_mode || 'multi')

const invokeStatusList = computed(() => {
  return Array.from(invokeStatusMap.value.values())
    .filter((item) => item.status !== 'completed')
    .sort((a, b) => b.updated_at - a.updated_at)
})

const agentBusy = computed(() => {
  if (invokeStatusList.value.length > 0) return true
  return Object.values(agentStore.typingStatus || {}).some((v) => !!v)
})

const coolingRoles = computed(() => {
  const now = Date.now()
  return Object.entries(cooldownUntilByRole.value)
    .filter(([, until]) => Number(until || 0) > now)
    .map(([role]) => role)
})

function roleLabel(role) {
  return ROLE_NAMES[role] || role
}

function parseRemainingSecondsFromMessage(message) {
  if (!message) return null
  const match = String(message).match(/(\d+)\s*秒/)
  if (!match) return null
  const sec = Number(match[1])
  if (!Number.isFinite(sec) || sec <= 0) return null
  return sec
}

function statusText(status, message) {
  if (message) return message
  const map = {
    accepted: 'accepted',
    queued: 'queued',
    thinking: 'thinking',
    failed: 'failed',
    unsupported: 'unsupported',
    completed: 'completed',
  }
  return map[status] || status
}

function statusClass(status) {
  const map = {
    accepted: 'border-blue-200 bg-blue-50 text-blue-700',
    queued: 'border-sky-200 bg-sky-50 text-sky-700',
    thinking: 'border-indigo-200 bg-indigo-50 text-indigo-700',
    failed: 'border-red-200 bg-red-50 text-red-700',
    unsupported: 'border-amber-200 bg-amber-50 text-amber-700',
    completed: 'border-green-200 bg-green-50 text-green-700',
  }
  return map[status] || 'border-gray-200 bg-gray-50 text-gray-700'
}

function upsertInvokeStatus(payload) {
  if (!payload?.source_message_id || !payload?.agent_role) return
  const key = `${payload.source_message_id}:${payload.agent_role}`
  const next = new Map(invokeStatusMap.value)
  next.set(key, {
    key,
    source_message_id: payload.source_message_id,
    agent_role: payload.agent_role,
    status: payload.status || 'accepted',
    message: payload.message || '',
    updated_at: Date.now(),
  })
  invokeStatusMap.value = next
}

function removeInvokeStatus(key) {
  const next = new Map(invokeStatusMap.value)
  next.delete(key)
  invokeStatusMap.value = next
}

function setTemporaryStatus(payload, delayMs = 5000) {
  if (!payload?.source_message_id || !payload?.agent_role) return
  upsertInvokeStatus(payload)
  const key = `${payload.source_message_id}:${payload.agent_role}`
  setTimeout(() => removeInvokeStatus(key), delayMs)
}

function scrollToBottom() {
  messageListRef.value?.scrollToBottom()
}

async function scrollToLatest() {
  unreadCount.value = 0
  await nextTick()
  scrollToBottom()
}

function handleViewportChange(payload) {
  isAtBottom.value = !!payload?.atBottom
  if (isAtBottom.value) unreadCount.value = 0
}

async function reloadHistory() {
  if (!roomId) return
  await chatStore.loadHistory(roomId)
  await nextTick()
  scrollToBottom()
}

async function handleLoadMore() {
  await chatStore.loadMore(roomId)
}

function handleSendMessage(content, mentions = []) {
  send({
    type: 'chat:message',
    content,
    mentions,
  })
}

onMounted(async () => {
  if (!roomId) return

  await reloadHistory()

  on('chat:new_message', async (data) => {
    const isSelfMessage = !!data.sender_id && data.sender_id === authStore.user?.id
    chatStore.addMessage(data)
    await nextTick()

    if (isSelfMessage || isAtBottom.value) {
      scrollToBottom()
      unreadCount.value = 0
      return
    }
    unreadCount.value += 1
  })

  on('agent:ack', (data) => {
    upsertInvokeStatus(data)
    if (data?.status === 'unsupported') setTemporaryStatus(data, 8000)
  })
  on('agent:queued', upsertInvokeStatus)
  on('agent:thinking', upsertInvokeStatus)
  on('agent:typing', handleTyping)

  on('agent:stream', async (data) => {
    const existed = chatStore.messages.some((m) => m.id === data.message_id)
    handleStream(data)
    await nextTick()
    if (isAtBottom.value) {
      scrollToBottom()
      unreadCount.value = 0
      return
    }
    if (!existed) unreadCount.value += 1
  })

  on('agent:stream_end', async (data) => {
    handleStreamEnd(data)
    if (data?.agent_role) {
      cooldownUntilByRole.value = {
        ...cooldownUntilByRole.value,
        [data.agent_role]: Date.now() + 5000,
      }
    }
    if (data?.source_message_id && data?.agent_role) {
      if (data.status === 'ok') {
        setTemporaryStatus(
          {
            source_message_id: data.source_message_id,
            agent_role: data.agent_role,
            status: 'completed',
            message: 'completed',
          },
          3000
        )
      } else {
        setTemporaryStatus(
          {
            source_message_id: data.source_message_id,
            agent_role: data.agent_role,
            status: 'failed',
            message: 'failed, please retry',
          },
          8000
        )
      }
    }
    await nextTick()
    if (isAtBottom.value) {
      scrollToBottom()
      unreadCount.value = 0
    }
  })

  on('agent:mention_blocked', (data) => {
    if (data?.reason === 'agent_cooling' && data?.agent_role) {
      const fromServer = parseRemainingSecondsFromMessage(data?.message)
      const remainingSeconds = fromServer ?? 5
      cooldownUntilByRole.value = {
        ...cooldownUntilByRole.value,
        [data.agent_role]: Date.now() + remainingSeconds * 1000,
      }
    }
    window.alert(data?.message || 'An agent is still busy, please try again in a moment.')
  })

  on('agent:queue_dropped', (data) => {
    if (!data?.source_message_id || !data?.agent_role) return
    setTemporaryStatus(
      {
        source_message_id: data.source_message_id,
        agent_role: data.agent_role,
        status: 'failed',
        message: data.message || 'agent task dropped, please retry',
      },
      8000
    )
  })

  on('room:user_join', (data) => {
    if (typeof data.online_count === 'number') onlineCount.value = data.online_count
  })
  on('room:user_leave', (data) => {
    if (typeof data.online_count === 'number') onlineCount.value = data.online_count
  })

  on('task_script:updated', async () => {
    try {
      await roomStore.loadTaskScriptState(roomId)
      await roomStore.loadTaskScriptLockState(roomId)
    } catch {
      // ignore transient sync failures
    }
  })
  on('room:timer_updated', (data) => roomStore.applyRoomTimerUpdate(data))
  on('room:writing_submit_updated', (data) => roomStore.applyWritingSubmitUpdate(data))

  on('__reconnect__', async () => {
    unreadCount.value = 0
    agentStore.clearTyping()
    invokeStatusMap.value = new Map()
    await reloadHistory()
    try {
      await roomStore.loadRoomContext(roomId, true)
    } catch {
      // ignore transient sync failures
    }
  })

  connect()
})

onUnmounted(() => {
  disconnect()
  chatStore.clearMessages()
  agentStore.clearTyping()
  invokeStatusMap.value = new Map()
})
</script>
