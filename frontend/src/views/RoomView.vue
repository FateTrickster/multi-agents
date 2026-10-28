<template>
  <div class="v2-room relative flex h-screen flex-col overflow-hidden bg-gray-50 pt-14">
    <div class="v2-room-topbar fixed top-0 left-0 right-0 z-40 h-14 border-b border-gray-200 bg-white px-4">
      <div class="h-full flex items-center justify-between gap-3">
        <div class="min-w-0">
          <p class="text-xs text-gray-500">{{ textTimerTitle }}</p>
          <p class="text-sm font-semibold" :class="timerTextClass">{{ timerDisplayText }}</p>
        </div>
        <div class="flex items-center gap-2">
          <button
            v-if="authStore.isTeacher"
            type="button"
            class="px-3 py-1.5 text-sm rounded-lg border transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
            :class="timerActionButtonClass"
            :disabled="roomStore.startingRoomTimer"
            @click="handleTimerAction"
          >
            {{ timerActionLabel }}
          </button>
          <button
            type="button"
            class="px-3 py-1.5 text-sm rounded-lg border border-red-200 text-red-600 bg-white hover:bg-red-50 transition-colors"
            @click="leaveRoom"
          >
            {{ textLeaveRoom }}
          </button>
        </div>
      </div>
    </div>

<main class="v2-room-grid">
      <section class="v2-room-col v2-room-writing" aria-label="共享写作区">
        <WritingArea class="v2-room-card flex-1" />
      </section>
      <section class="v2-room-col v2-room-chat" aria-label="小组讨论区">
        <ChatPanel class="v2-room-card flex-1" />
      </section>
      <aside class="v2-room-col v2-room-task">
      <TaskRequirements
        class="v2-room-card flex-1 min-h-0"
        :task="roomStore.currentTask"
        :loading="roomStore.loadingContext"
        :error="roomStore.contextError"
        :editable="authStore.isTeacher"
        :saving="roomStore.savingTask"
        :save-error="roomStore.taskSaveError"
        :save-success="taskSaveSuccess"
        @save="saveTaskPatch"
      />
      <TaskScript
        v-if="showMultiAgentTaskFlow"
        class="v2-room-card flex-1 min-h-0"
        :state="roomStore.taskScriptState"
        :loading="roomStore.loadingTaskScript"
        :error="roomStore.taskScriptError"
        :lock-notice="roomStore.taskScriptLockNotice || roomStore.taskScriptLockError"
        :can-confirm="authStore.user?.role === 'student'"
        :requesting-proposal="roomStore.requestingTaskScriptProposal"
        :confirming-proposal="roomStore.confirmingTaskScriptProposal"
        :lock-state="roomStore.taskScriptLockState"
        :has-edit-lock="roomStore.isTaskScriptLockMine"
        :acquiring-lock="roomStore.acquiringTaskScriptLock"
        :releasing-lock="roomStore.releasingTaskScriptLock"
        :current-user-id="authStore.user?.id || ''"
        @request-proposal="requestFacilitatorProposal"
        @confirm-proposal="confirmTaskScriptProposal"
        @acquire-lock="acquireTaskScriptLock"
        @release-lock="releaseTaskScriptLock"
      />
    </aside>
    </main>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth.js'
import { useRoomStore } from '../stores/room.js'
import WritingArea from '../components/layout/WritingArea.vue'
import ChatPanel from '../components/chat/ChatPanel.vue'
import TaskRequirements from '../components/task/TaskRequirements.vue'
import TaskScript from '../components/task/TaskScript.vue'

const TEXT_TIMER_TITLE = '\u4efb\u52a1\u8ba1\u65f6'
const TEXT_LEAVE_ROOM = '\u9000\u51fa\u623f\u95f4'
const TEXT_TIMER_NOT_STARTED = '\u8ba1\u65f6\u672a\u5f00\u59cb'
const TEXT_TIME_REMAINING = '\u5269\u4f59'
const TEXT_OVERTIME = '\u5df2\u8d85\u65f6'
const TEXT_START_TIMER = '\u5f00\u59cb\u8ba1\u65f6'
const TEXT_RESET_TIMER = '\u91cd\u7f6e\u8ba1\u65f6'
const DEFAULT_ROOM_TIMER_DURATION_MINUTES = 120
const TEXT_CONFIRM_START = `\u786e\u8ba4\u5f00\u59cb${DEFAULT_ROOM_TIMER_DURATION_MINUTES}\u5206\u949f\u8ba1\u65f6\uff1f`
const TEXT_CONFIRM_RESET = '\u786e\u8ba4\u91cd\u7f6e\u8ba1\u65f6\uff1f\u91cd\u7f6e\u540e\u5c06\u56de\u5230\u201c\u8ba1\u65f6\u672a\u5f00\u59cb\u201d\u3002'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const roomStore = useRoomStore()
const nowMs = ref(Date.now())
const taskSaveSuccess = computed(
  () => !!roomStore.taskSaveSuccessAt && !roomStore.savingTask && !roomStore.taskSaveError
)
let lockRenewTimer = null
let timerTick = null

const textTimerTitle = TEXT_TIMER_TITLE
const textLeaveRoom = TEXT_LEAVE_ROOM

const timerDiffMs = computed(() => {
  if (!roomStore.timerStartedAt || !roomStore.timerDeadlineAt) return null
  const deadline = Date.parse(roomStore.timerDeadlineAt)
  if (Number.isNaN(deadline)) return null
  const stoppedAt = roomStore.timerStoppedAt ? Date.parse(roomStore.timerStoppedAt) : NaN
  const referenceNowMs = Number.isNaN(stoppedAt) ? nowMs.value : stoppedAt
  return deadline - referenceNowMs
})

const timerDisplayText = computed(() => {
  if (timerDiffMs.value == null) return TEXT_TIMER_NOT_STARTED
  if (timerDiffMs.value >= 0) return `${TEXT_TIME_REMAINING} ${formatDuration(timerDiffMs.value)}`
  return `${TEXT_OVERTIME} +${formatDuration(Math.abs(timerDiffMs.value))}`
})

const timerTextClass = computed(() => {
  if (timerDiffMs.value == null) return 'text-gray-700'
  if (timerDiffMs.value < 0) return 'text-red-600'
  if (timerDiffMs.value <= 10 * 60 * 1000) return 'text-orange-600'
  return 'text-gray-800'
})

const timerActionLabel = computed(() => {
  if (!roomStore.timerStartedAt || !roomStore.timerDeadlineAt) return TEXT_START_TIMER
  return TEXT_RESET_TIMER
})

const timerActionButtonClass = computed(() => {
  if (!roomStore.timerStartedAt || !roomStore.timerDeadlineAt) {
    return 'border-blue-200 text-blue-600 bg-white hover:bg-blue-50'
  }
  return 'border-amber-200 text-amber-700 bg-white hover:bg-amber-50'
})
const currentAgentMode = computed(() => roomStore.currentRoom?.agent_mode || 'multi')
const showMultiAgentTaskFlow = computed(() => currentAgentMode.value === 'multi')

function formatDuration(ms) {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000))
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60

  if (hours > 0) {
    return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
  }
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

function loadRoomContext() {
  const roomId = String(route.params.id || '')
  if (!roomId) return
  const previousRoomId = roomStore.currentRoomId
  if (previousRoomId && previousRoomId !== roomId && roomStore.isTaskScriptLockMine) {
    roomStore.releaseTaskScriptLock().catch(() => {})
  }
  roomStore.loadRoomContext(roomId)
}

function leaveRoom() {
  router.push('/lobby')
}

async function handleTimerAction() {
  if (!authStore.isTeacher) return

  const hasStarted = !!roomStore.timerStartedAt && !!roomStore.timerDeadlineAt
  const confirmText = hasStarted ? TEXT_CONFIRM_RESET : TEXT_CONFIRM_START
  if (!window.confirm(confirmText)) return

  if (hasStarted) {
    await roomStore.resetRoomTimer(String(route.params.id || ''))
  } else {
    await roomStore.startRoomTimer(String(route.params.id || ''))
  }
}

async function saveTaskPatch(patch) {
  if (!authStore.isTeacher) return
  await roomStore.updateCurrentTask(patch)
}

async function requestFacilitatorProposal() {
  await roomStore.requestFacilitatorTaskScriptProposal()
}

async function acquireTaskScriptLock() {
  if (authStore.user?.role !== 'student') return
  await roomStore.acquireTaskScriptLock()
}

async function releaseTaskScriptLock() {
  if (authStore.user?.role !== 'student') return
  if (!roomStore.isTaskScriptLockMine) return
  await roomStore.releaseTaskScriptLock()
}

async function confirmTaskScriptProposal(payload) {
  if (authStore.user?.role !== 'student') return
  const requestPayload = {
    ...(payload || {}),
  }
  if (roomStore.ownTaskScriptLeaseId) {
    requestPayload.lease_id = roomStore.ownTaskScriptLeaseId
  }
  await roomStore.confirmTaskScriptProposal(requestPayload)
}

onMounted(loadRoomContext)
onMounted(() => {
  timerTick = setInterval(() => {
    nowMs.value = Date.now()
  }, 1000)
})

watch(() => route.params.id, loadRoomContext)

watch(
  () => [roomStore.currentRoomId, roomStore.isTaskScriptLockMine, roomStore.ownTaskScriptLeaseId],
  ([roomId, isMine, leaseId]) => {
    if (lockRenewTimer) {
      clearInterval(lockRenewTimer)
      lockRenewTimer = null
    }
    if (!roomId || !isMine || !leaseId) return
    lockRenewTimer = setInterval(async () => {
      try {
        await roomStore.renewTaskScriptLock()
      } catch (error) {
        // lock loss is surfaced by store error state; no-op here.
      }
    }, 30000)
  },
  { immediate: true }
)

watch(
  () => roomStore.taskScriptState?.pending_proposal?.id || null,
  async (pendingId) => {
    if (pendingId) return
    if (roomStore.isTaskScriptLockMine) {
      try {
        await roomStore.releaseTaskScriptLock()
      } catch (error) {
        // ignore best-effort release.
      }
    }
  }
)

onUnmounted(() => {
  if (timerTick) {
    clearInterval(timerTick)
    timerTick = null
  }
  if (lockRenewTimer) {
    clearInterval(lockRenewTimer)
    lockRenewTimer = null
  }
  if (roomStore.isTaskScriptLockMine) {
    roomStore.releaseTaskScriptLock().catch(() => {})
  }
})
</script>
