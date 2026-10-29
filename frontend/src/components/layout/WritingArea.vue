<template>
  <div class="flex h-full flex-col rounded-lg border border-gray-200 bg-white p-3">
    <div class="mb-2 flex items-center justify-between">
      <p class="text-xs font-semibold uppercase tracking-wide text-gray-400">写作区</p>
      <span class="text-xs text-gray-400">{{ wordCount }} 字</span>
    </div>

    <div class="mb-2 flex flex-wrap items-center gap-1 rounded-md border border-gray-200 bg-gray-50 p-1.5">
      <button
        type="button"
        class="rounded border border-gray-200 bg-white px-2 py-1 text-xs text-gray-600 hover:bg-gray-100"
        @mousedown.prevent="applyFormatBlock('p')"
      >
        正文
      </button>
      <button
        type="button"
        class="rounded border border-gray-200 bg-white px-2 py-1 text-xs text-gray-600 hover:bg-gray-100"
        @mousedown.prevent="applyFormatBlock('h1')"
      >
        H1
      </button>
      <button
        type="button"
        class="rounded border border-gray-200 bg-white px-2 py-1 text-xs text-gray-600 hover:bg-gray-100"
        @mousedown.prevent="applyFormatBlock('h2')"
      >
        H2
      </button>
      <button
        type="button"
        class="rounded border border-gray-200 bg-white px-2 py-1 text-xs text-gray-600 hover:bg-gray-100"
        @mousedown.prevent="applyInlineFormat('strong')"
      >
        加粗
      </button>
      <button
        type="button"
        class="rounded border border-gray-200 bg-white px-2 py-1 text-xs text-gray-600 hover:bg-gray-100"
        @mousedown.prevent="applyInlineFormat('em')"
      >
        斜体
      </button>
      <button
        type="button"
        class="rounded border border-gray-200 bg-white px-2 py-1 text-xs text-gray-600 hover:bg-gray-100"
        @mousedown.prevent="applyInlineFormat('u')"
      >
        下划线
      </button>
      <button
        type="button"
        class="rounded border border-gray-200 bg-white px-2 py-1 text-xs text-gray-600 hover:bg-gray-100"
        @mousedown.prevent="applyCommand('insertUnorderedList')"
      >
        无序列表
      </button>
      <button
        type="button"
        class="rounded border border-gray-200 bg-white px-2 py-1 text-xs text-gray-600 hover:bg-gray-100"
        @mousedown.prevent="applyCommand('insertOrderedList')"
      >
        有序列表
      </button>
      <button
        type="button"
        class="rounded border border-gray-200 bg-white px-2 py-1 text-xs text-gray-600 hover:bg-gray-100"
        @mousedown.prevent="applyCommand('justifyLeft')"
      >
        左对齐
      </button>
      <button
        type="button"
        class="rounded border border-gray-200 bg-white px-2 py-1 text-xs text-gray-600 hover:bg-gray-100"
        @mousedown.prevent="applyCommand('justifyCenter')"
      >
        居中
      </button>
      <button
        type="button"
        class="rounded border border-gray-200 bg-white px-2 py-1 text-xs text-gray-600 hover:bg-gray-100"
        @mousedown.prevent="applyCommand('justifyRight')"
      >
        右对齐
      </button>
      <button
        type="button"
        class="ml-1 inline-flex items-center gap-2 rounded border border-gray-200 bg-white px-2 py-1 text-xs text-gray-600 hover:bg-gray-100"
        @mousedown.prevent="applyColor"
      >
        <span>颜色</span>
        <input
          v-model="currentColor"
          type="color"
          class="h-4 w-5 cursor-pointer rounded border border-gray-200 bg-white p-0"
          @mousedown.stop="saveSelection"
          @click.stop
          @input.stop="handleColorChange"
        />
      </button>
      <button
        type="button"
        class="rounded border border-gray-200 bg-white px-2 py-1 text-xs text-gray-600 hover:bg-gray-100"
        @mousedown.prevent="clearFormat"
      >
        清除格式
      </button>
      <button
        type="button"
        class="rounded border border-gray-200 bg-white px-2 py-1 text-xs text-gray-600 hover:bg-gray-100"
        @mousedown.prevent="undoOnce"
      >
        撤销
      </button>
      <button
        type="button"
        class="rounded border border-gray-200 bg-white px-2 py-1 text-xs text-gray-600 hover:bg-gray-100"
        @mousedown.prevent="redoOnce"
      >
        重做
      </button>
      <button
        class="ml-auto rounded border border-emerald-200 bg-white px-2 py-1 text-xs text-emerald-600 hover:bg-emerald-50 disabled:opacity-60"
        v-if="isStudent"
        type="button"
        :disabled="savingVersion"
        @click="saveVersion"
      >
        {{ savingVersion ? '保存中...' : '保存' }}
      </button>
    </div>

    <div class="mb-2 rounded border border-sky-200 bg-sky-50 px-2 py-1 text-[11px] text-sky-700">
      <span v-if="otherEditingNames.length === 0">当前仅你在编辑或暂无其他同学编辑</span>
      <span v-else>正在编辑：{{ otherEditingNames.join('、') }}</span>
    </div>

    <div
      ref="editorRef"
      class="writing-editor min-h-0 flex-1 overflow-auto rounded-md border border-gray-200 p-3 text-sm text-gray-700 outline-none focus:border-blue-400"
      contenteditable="true"
      @input="handleInput"
      @keydown="handleKeydown"
      @mouseup="saveSelection"
      @keyup="saveSelection"
      @paste="handlePaste"
      @blur="handleBlur"
      @focus="handleFocus"
    ></div>

    <div class="mt-2 rounded-md border border-amber-200 bg-amber-50 px-2 py-1.5 text-[11px] text-amber-700">
      提交进度：{{ confirmCount }}/{{ requiredConfirmations }} 人确认
      <span v-if="isMeConfirmed">（你已确认）</span>
      <span v-if="isFinalSubmitted">，已最终提交</span>
    </div>

    <div class="mt-2 flex items-center justify-between">
      <p class="text-xs text-gray-400">{{ saveHint }}</p>
      <button
        type="button"
        class="rounded-md px-3 py-1.5 text-xs text-white disabled:opacity-60"
        :class="isFinalSubmitted ? 'bg-green-600' : 'bg-blue-600 hover:bg-blue-700'"
        :disabled="!canConfirmSubmit"
        @click="confirmSubmit"
      >
        {{ submitButtonText }}
      </button>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from '../../stores/auth.js'
import { useRoomStore } from '../../stores/room.js'
import { roomApi } from '../../services/roomApi.js'
import { useWebSocket } from '../../composables/useWebSocket.js'

const route = useRoute()
const authStore = useAuthStore()
const roomStore = useRoomStore()
const roomId = String(route.params.id || '')
const { connect, on, send, disconnect, connected } = useWebSocket(roomId)

const editorRef = ref(null)
const htmlDraft = ref('')
const plainText = ref('')
const lastSavedAt = ref(null)
const currentColor = ref('#1f2937')
const savedRange = ref(null)
const lastActivityReportAt = ref(0)
const lastLocalEditAt = ref(0)
const lastAwarenessSentAt = ref(0)
const docVersion = ref(0)
const awarenessMap = ref({})
const savingVersion = ref(false)
const undoStack = ref([])
const redoStack = ref([])

const ACTIVITY_REPORT_INTERVAL_MS = 12000
const AWARENESS_SEND_INTERVAL_MS = 2000
const AWARENESS_STALE_MS = 8000
const UNDO_STACK_MAX = 50

let wsSendTimer = null
let awarenessCleaner = null
let awarenessHeartbeat = null

const storageKey = computed(() => `room:${roomId}:writing_draft`)

const writingState = computed(() => {
  return (
    roomStore.writingSubmitState || {
      required_confirmations: 1,
      confirmations: [],
      final_submitted_at: null,
    }
  )
})

const requiredConfirmations = computed(() => Number(writingState.value.required_confirmations || 1))
const confirmations = computed(() => (Array.isArray(writingState.value.confirmations) ? writingState.value.confirmations : []))
const confirmCount = computed(() => confirmations.value.length)
const isFinalSubmitted = computed(() => !!writingState.value.final_submitted_at)
const isStudent = computed(() => authStore.user?.role === 'student')
const myUserId = computed(() => String(authStore.user?.id || ''))
const timerStarted = computed(() => !!roomStore.timerStartedAt)
const MIN_WRITING_SUBMIT_CHARS = 50

const isMeConfirmed = computed(() => {
  if (!myUserId.value) return false
  return confirmations.value.some((item) => String(item?.user_id || '') === myUserId.value)
})

const wordCount = computed(() => plainText.value.trim().length)

const otherEditingNames = computed(() => {
  const now = Date.now()
  return Object.values(awarenessMap.value)
    .filter((item) => item && item.user_id !== myUserId.value)
    .filter((item) => !!item.is_editing && now - Number(item.local_ts || 0) <= AWARENESS_STALE_MS)
    .map((item) => item.display_name || '同学')
})

const saveHint = computed(() => {
  if (!lastSavedAt.value) return connected.value ? '实时同步已开启' : '同步连接中...'
  return `最近同步：${lastSavedAt.value}`
})

const submitDisplay = computed(() => {
  if (!writingState.value.final_submitted_at) return ''
  const d = new Date(writingState.value.final_submitted_at)
  if (Number.isNaN(d.getTime())) return ''
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
})

const canConfirmSubmit = computed(() => {
  if (isFinalSubmitted.value) return false
  if (!isStudent.value) return false
  if (!timerStarted.value) return false
  if (wordCount.value < MIN_WRITING_SUBMIT_CHARS) return false
  return !roomStore.confirmingWritingSubmit
})

const submitButtonText = computed(() => {
  if (isFinalSubmitted.value) return `已提交 ${submitDisplay.value}`
  if (!timerStarted.value) return '计时未开始，暂不可提交'
  if (wordCount.value < MIN_WRITING_SUBMIT_CHARS) {
    return `至少 ${MIN_WRITING_SUBMIT_CHARS} 字（当前 ${wordCount.value} 字）`
  }
  if (roomStore.confirmingWritingSubmit) return '确认中...'
  if (isMeConfirmed.value) return '取消确认'
  return '确认提交答题'
})

function extractPlainText(html) {
  const el = document.createElement('div')
  el.innerHTML = html || ''
  return el.textContent || ''
}

function saveDraft() {
  localStorage.setItem(storageKey.value, htmlDraft.value)
  const now = new Date()
  lastSavedAt.value = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`
}

function applyContent(content) {
  htmlDraft.value = content || ''
  plainText.value = extractPlainText(htmlDraft.value)
  if (editorRef.value && editorRef.value.innerHTML !== htmlDraft.value) {
    editorRef.value.innerHTML = htmlDraft.value
  }
  saveDraft()
}

function resetUndoHistory(content) {
  undoStack.value = [String(content || '')]
  redoStack.value = []
}

function recordUndoSnapshot(content) {
  const text = String(content || '')
  const last = undoStack.value.length ? undoStack.value[undoStack.value.length - 1] : null
  if (last === text) return
  undoStack.value.push(text)
  if (undoStack.value.length > UNDO_STACK_MAX) {
    undoStack.value = undoStack.value.slice(undoStack.value.length - UNDO_STACK_MAX)
  }
  redoStack.value = []
}

function reportWritingActivity() {
  if (!roomId) return
  const nowMs = Date.now()
  if (nowMs - lastActivityReportAt.value < ACTIVITY_REPORT_INTERVAL_MS) return
  lastActivityReportAt.value = nowMs
  roomApi.reportRoomActivity(roomId, 'writing').catch(() => {})
}

function sendAwareness(isEditing) {
  if (!roomId || !myUserId.value) return
  const nowMs = Date.now()
  if (nowMs - lastAwarenessSentAt.value < AWARENESS_SEND_INTERVAL_MS && isEditing) return
  lastAwarenessSentAt.value = nowMs
  send({
    type: 'writing:awareness',
    is_editing: !!isEditing,
    cursor: null,
  })
}

function scheduleSendUpdate() {
  if (!roomId) return
  if (wsSendTimer) {
    clearTimeout(wsSendTimer)
  }
  wsSendTimer = setTimeout(() => {
    wsSendTimer = null
    send({
      type: 'writing:update',
      content: htmlDraft.value,
      base_version: docVersion.value,
    })
  }, 120)
}

function handleInput() {
  if (!editorRef.value) return
  htmlDraft.value = editorRef.value.innerHTML
  plainText.value = editorRef.value.innerText || ''
  lastLocalEditAt.value = Date.now()
  saveDraft()
  recordUndoSnapshot(htmlDraft.value)
  reportWritingActivity()
  sendAwareness(true)
  scheduleSendUpdate()
}

function applyUndoContent(content) {
  applyContent(content)
  lastLocalEditAt.value = Date.now()
  scheduleSendUpdate()
}

function undoOnce() {
  if (undoStack.value.length <= 1) return
  const current = undoStack.value.pop()
  redoStack.value.push(current)
  const prev = undoStack.value[undoStack.value.length - 1] || ''
  applyUndoContent(prev)
}

function redoOnce() {
  if (!redoStack.value.length) return
  const next = redoStack.value.pop()
  undoStack.value.push(next)
  if (undoStack.value.length > UNDO_STACK_MAX) {
    undoStack.value = undoStack.value.slice(undoStack.value.length - UNDO_STACK_MAX)
  }
  applyUndoContent(next)
}

function handleKeydown(event) {
  const ctrlOrMeta = event.ctrlKey || event.metaKey
  if (!ctrlOrMeta) return
  const key = String(event.key || '').toLowerCase()
  if (key === 'z' && !event.shiftKey) {
    event.preventDefault()
    undoOnce()
    return
  }
  if ((key === 'z' && event.shiftKey) || key === 'y') {
    event.preventDefault()
    redoOnce()
  }
}

function handleFocus() {
  saveSelection()
  sendAwareness(true)
}

function handleBlur() {
  saveSelection()
  sendAwareness(false)
}

function focusEditor() {
  editorRef.value?.focus()
}

function saveSelection() {
  const editor = editorRef.value
  const selection = window.getSelection()
  if (!editor || !selection || selection.rangeCount === 0) return

  const range = selection.getRangeAt(0)
  if (!editor.contains(range.startContainer) || !editor.contains(range.endContainer)) return
  savedRange.value = range.cloneRange()
}

function isSelectionInsideEditor(selection = window.getSelection()) {
  const editor = editorRef.value
  if (!editor || !selection || selection.rangeCount === 0) return false
  const range = selection.getRangeAt(0)
  return editor.contains(range.startContainer) && editor.contains(range.endContainer)
}

function captureCurrentSelection() {
  const selection = window.getSelection()
  if (!isSelectionInsideEditor(selection)) return false
  savedRange.value = selection.getRangeAt(0).cloneRange()
  return true
}

function handleSelectionChange() {
  captureCurrentSelection()
}

function placeCaretAtEnd() {
  const editor = editorRef.value
  if (!editor) return
  const range = document.createRange()
  range.selectNodeContents(editor)
  range.collapse(false)
  const selection = window.getSelection()
  if (!selection) return
  selection.removeAllRanges()
  selection.addRange(range)
  savedRange.value = range.cloneRange()
}

function restoreSelection() {
  const editor = editorRef.value
  if (!editor) return

  focusEditor()
  const selection = window.getSelection()
  if (!selection) return

  if (savedRange.value) {
    selection.removeAllRanges()
    selection.addRange(savedRange.value)
  } else {
    placeCaretAtEnd()
  }
}

function applyCommand(command, value = null) {
  const captured = captureCurrentSelection()
  if (!captured) {
    restoreSelection()
  }
  document.execCommand(command, false, value)
  saveSelection()
  handleInput()
  focusEditor()
}

function applyFormatBlock(tag) {
  applyCommand('formatBlock', String(tag || 'p').toLowerCase())
}

function getCurrentRangeForInline() {
  const captured = captureCurrentSelection()
  if (!captured) {
    restoreSelection()
  }
  const selection = window.getSelection()
  const range = selection && selection.rangeCount > 0 ? selection.getRangeAt(0) : null
  if (!range || range.collapsed) return null
  if (!isSelectionInsideEditor(selection)) return null
  return range
}

function getInlineFormatConfig(format) {
  const key = String(format || '').toLowerCase()
  if (key === 'strong') {
    return { tagName: 'strong', aliases: ['STRONG', 'B'] }
  }
  if (key === 'em') {
    return { tagName: 'em', aliases: ['EM', 'I'] }
  }
  if (key === 'u') {
    return { tagName: 'u', aliases: ['U'] }
  }
  return null
}

function findClosestInlineAncestor(node, format) {
  const editor = editorRef.value
  const cfg = getInlineFormatConfig(format)
  if (!editor || !cfg || !node) return null

  let current = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement
  while (current && current !== editor) {
    if (current.nodeType === Node.ELEMENT_NODE && cfg.aliases.includes(current.tagName)) {
      return current
    }
    current = current.parentElement
  }
  return null
}

function isRangeFullyInsideSingleInline(range, format) {
  if (!range || range.collapsed) return null
  const startInline = findClosestInlineAncestor(range.startContainer, format)
  if (!startInline) return null
  const endInline = findClosestInlineAncestor(range.endContainer, format)
  if (!endInline || endInline !== startInline) return null

  const editor = editorRef.value
  if (!editor || !editor.contains(startInline)) return null

  const normalizeText = (text) => String(text || '').replace(/\s+/g, ' ').trim()
  const selectedText = normalizeText(range.toString())
  if (!selectedText) return null
  const inlineText = normalizeText(startInline.textContent || '')

  return selectedText === inlineText ? startInline : null
}

function unwrapInlineElement(element) {
  const parent = element?.parentNode
  if (!parent) return null

  let firstMoved = null
  let lastMoved = null
  while (element.firstChild) {
    const child = element.firstChild
    if (!firstMoved) firstMoved = child
    lastMoved = child
    parent.insertBefore(child, element)
  }
  parent.removeChild(element)

  return { parent, firstMoved, lastMoved }
}

function wrapSelectionWithInline(tagName, options = {}) {
  const range = getCurrentRangeForInline()
  if (!range) return

  const fragment = range.extractContents()
  if (!fragment || !fragment.textContent) return

  const wrapper = document.createElement(tagName)
  if (options.style && typeof options.style === 'object') {
    Object.entries(options.style).forEach(([key, value]) => {
      if (value !== undefined && value !== null) {
        wrapper.style[key] = String(value)
      }
    })
  }

  wrapper.appendChild(fragment)
  range.insertNode(wrapper)

  const selection = window.getSelection()
  if (selection) {
    const newRange = document.createRange()
    newRange.selectNodeContents(wrapper)
    selection.removeAllRanges()
    selection.addRange(newRange)
  }

  saveSelection()
  handleInput()
  focusEditor()
}

function applyInlineFormat(tagName) {
  const range = getCurrentRangeForInline()
  if (!range) return

  const inlineEl = isRangeFullyInsideSingleInline(range, tagName)
  if (inlineEl) {
    const moved = unwrapInlineElement(inlineEl)
    if (moved?.firstMoved && moved?.lastMoved) {
      const selection = window.getSelection()
      if (selection) {
        const nextRange = document.createRange()
        nextRange.setStartBefore(moved.firstMoved)
        nextRange.setEndAfter(moved.lastMoved)
        selection.removeAllRanges()
        selection.addRange(nextRange)
      }
    }
    saveSelection()
    handleInput()
    focusEditor()
    return
  }

  wrapSelectionWithInline(tagName)
}

function applyColor() {
  wrapSelectionWithInline('span', { style: { color: currentColor.value } })
}

function handleColorChange() {
  saveSelection()
}

function clearFormat() {
  restoreSelection()
  document.execCommand('removeFormat', false, null)
  saveSelection()
  handleInput()
}

function handlePaste(event) {
  event.preventDefault()
  const text = event.clipboardData?.getData('text/plain') || ''
  if (!text) return

  const captured = captureCurrentSelection()
  if (!captured) {
    restoreSelection()
  }

  const selection = window.getSelection()
  if (!selection || selection.rangeCount === 0) return
  const range = selection.getRangeAt(0)

  range.deleteContents()

  const lines = text.split(/\r\n|\r|\n/)
  const fragment = document.createDocumentFragment()
  let lastInsertedNode = null
  lines.forEach((line, index) => {
    const textNode = document.createTextNode(line)
    fragment.appendChild(textNode)
    lastInsertedNode = textNode
    if (index < lines.length - 1) {
      const br = document.createElement('br')
      fragment.appendChild(br)
      lastInsertedNode = br
    }
  })

  range.insertNode(fragment)

  const tailRange = document.createRange()
  if (lastInsertedNode) {
    tailRange.setStartAfter(lastInsertedNode)
  } else {
    tailRange.setStart(range.endContainer, range.endOffset)
  }
  tailRange.collapse(true)
  selection.removeAllRanges()
  selection.addRange(tailRange)

  saveSelection()
  handleInput()
  focusEditor()
}

async function confirmSubmit() {
  if (!roomId) return
  try {
    await roomStore.confirmWritingSubmit(roomId)
  } catch (error) {
    const detail = error?.response?.data?.detail
    const message =
      (typeof detail === 'object' && detail?.message) ||
      (typeof detail === 'string' && detail) ||
      '当前不满足提交条件'
    window.alert(message)
    return
  }
  reportWritingActivity()
}

async function initWritingDoc() {
  const localDraft = localStorage.getItem(storageKey.value) || ''
  try {
    const state = await roomApi.getWritingDocState(roomId)
    docVersion.value = Number(state?.version || 0)
    if (docVersion.value === 0 && localDraft) {
      applyContent(localDraft)
      resetUndoHistory(localDraft)
      scheduleSendUpdate()
    } else {
      applyContent(String(state?.content || ''))
      resetUndoHistory(String(state?.content || ''))
    }
  } catch {
    applyContent(localDraft)
    resetUndoHistory(localDraft)
  }
}

async function saveVersion() {
  if (!roomId || !isStudent.value || savingVersion.value) return
  savingVersion.value = true
  try {
    await roomApi.saveWritingDocVersion(roomId)
  } finally {
    savingVersion.value = false
  }
}

onMounted(async () => {
  if (!roomId) return

  document.addEventListener('selectionchange', handleSelectionChange)

  on('writing:updated', (data) => {
    const incomingVersion = Number(data?.version || 0)
    const updatedBy = String(data?.updated_by || '')
    if (incomingVersion <= docVersion.value) return

    docVersion.value = incomingVersion

    if (updatedBy !== myUserId.value && Date.now() - lastLocalEditAt.value > 250) {
      applyContent(String(data?.content || ''))
      resetUndoHistory(String(data?.content || ''))
    }
  })

  on('writing:resync', (data) => {
    const incomingVersion = Number(data?.version || 0)
    if (incomingVersion <= docVersion.value) return
    docVersion.value = incomingVersion
    applyContent(String(data?.content || ''))
    resetUndoHistory(String(data?.content || ''))
  })

  on('writing:awareness', (data) => {
    const userId = String(data?.user_id || '')
    if (!userId) return
    awarenessMap.value = {
      ...awarenessMap.value,
      [userId]: {
        user_id: userId,
        display_name: data?.display_name || '同学',
        is_editing: !!data?.is_editing,
        local_ts: Date.now(),
      },
    }
  })

  connect()
  await initWritingDoc()
  await roomStore.loadWritingSubmitState(roomId)

  awarenessCleaner = setInterval(() => {
    const now = Date.now()
    const next = { ...awarenessMap.value }
    Object.keys(next).forEach((uid) => {
      const ts = Number(next[uid]?.local_ts || 0)
      if (!ts || now - ts > AWARENESS_STALE_MS) {
        delete next[uid]
      }
    })
    awarenessMap.value = next
  }, 2000)

  awarenessHeartbeat = setInterval(() => {
    if (document.activeElement === editorRef.value) {
      sendAwareness(true)
    }
  }, 2000)
})

onUnmounted(() => {
  document.removeEventListener('selectionchange', handleSelectionChange)
  sendAwareness(false)
  if (wsSendTimer) {
    clearTimeout(wsSendTimer)
    wsSendTimer = null
  }
  if (awarenessCleaner) {
    clearInterval(awarenessCleaner)
    awarenessCleaner = null
  }
  if (awarenessHeartbeat) {
    clearInterval(awarenessHeartbeat)
    awarenessHeartbeat = null
  }
  disconnect()
})

watch(editorRef, (el) => {
  if (!el) return
  el.innerHTML = htmlDraft.value || ''
})
</script>

<style scoped>
.writing-editor:empty::before {
  content: '在这里整理观点、记录结论或起草最终答案...';
  color: #9ca3af;
}

.writing-editor :deep(h1) {
  margin: 0.6rem 0 0.4rem;
  font-size: 1.25rem;
  font-weight: 700;
  line-height: 1.4;
}

.writing-editor :deep(h2) {
  margin: 0.55rem 0 0.35rem;
  font-size: 1.1rem;
  font-weight: 700;
  line-height: 1.4;
}

.writing-editor :deep(p) {
  margin: 0.35rem 0;
  line-height: 1.7;
}

.writing-editor :deep(ul),
.writing-editor :deep(ol) {
  margin: 0.35rem 0 0.35rem 1.25rem;
  padding-left: 0.75rem;
}

.writing-editor :deep(ul) {
  list-style: disc;
}

.writing-editor :deep(ol) {
  list-style: decimal;
}

.writing-editor :deep(li) {
  margin: 0.2rem 0;
  line-height: 1.6;
}
</style>
