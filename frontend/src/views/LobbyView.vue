<template>
  <div class="v2-lobby min-h-screen">
    <header class="v2-lobby-header flex flex-wrap items-center justify-between gap-3 bg-white px-6 py-4">
      <h1 class="text-lg font-bold text-gray-800">{{ TXT.lobbyTitle }}</h1>
      <div class="flex items-center gap-4">
        <span class="text-sm text-gray-600">
          {{ authStore.user?.display_name }}（{{ authStore.isTeacher ? TXT.teacher : TXT.student }}）
        </span>
        <button class="text-sm text-red-500 hover:underline" @click="logout">{{ TXT.logout }}</button>
      </div>
    </header>

    <main class="v2-lobby-main mx-auto max-w-5xl px-4 py-8 sm:px-6">
      <div v-if="authStore.isTeacher" class="mb-6 flex flex-wrap justify-end gap-3">
        <button class="rounded-lg border border-blue-200 bg-white px-4 py-2 text-blue-700 hover:bg-blue-50" @click="openTemplateModal">
          + {{ TXT.createTemplate }}
        </button>
        <button class="rounded-lg border border-indigo-200 bg-white px-4 py-2 text-indigo-700 hover:bg-indigo-50" @click="openTemplateManagerModal">
          {{ TXT.manageTemplates }}
        </button>
        <button class="rounded-lg border border-emerald-200 bg-white px-4 py-2 text-emerald-700 hover:bg-emerald-50" @click="goTeacherExports">
          {{ TXT.exportData }}
        </button>
        <button class="rounded-lg bg-blue-600 px-4 py-2 text-white hover:bg-blue-700" @click="openCreateModal">
          + {{ TXT.createRoom }}
        </button>
      </div>

      <div v-if="authStore.isTeacher" class="mb-4 grid grid-cols-1 gap-3 rounded-xl border border-gray-200 bg-white p-4 md:grid-cols-[1fr_220px]">
        <input
          v-model="roomSearchQuery"
          type="text"
          class="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          :placeholder="TXT.searchRoomPlaceholder"
        />
        <select
          v-model="roomStatusFilter"
          class="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          <option value="all">{{ TXT.filterAll }}</option>
          <option value="not_started">{{ TXT.statusNotStarted }}</option>
          <option value="running">{{ TXT.statusRunning }}</option>
          <option value="ended">{{ TXT.statusEnded }}</option>
        </select>
      </div>

      <div v-if="roomsLoading" class="py-16 text-center text-gray-400">{{ TXT.loadingRooms }}</div>
      <div v-else-if="roomsError" class="py-16 text-center text-red-500">{{ roomsError }}</div>
      <div v-else-if="filteredRooms.length === 0" class="py-16 text-center text-gray-400">{{ TXT.noRooms }}</div>
      <div v-else class="grid grid-cols-1 gap-4">
        <div v-for="room in filteredRooms" :key="room.id" class="flex items-center justify-between rounded-xl bg-white p-5 shadow-sm">
          <div>
            <p class="font-medium text-gray-800">{{ room.name }}</p>
            <div class="mt-1 flex items-center gap-2">
              <span
                class="rounded-full px-2 py-0.5 text-xs"
                :class="roomStatusClass(room)"
              >
                {{ roomStatusText(room) }}
              </span>
              <span class="text-xs text-gray-400">{{ room.online_count ?? room.member_count }} {{ TXT.onlineMembers }}</span>
            </div>
          </div>

          <div class="flex items-center gap-2">
            <button class="rounded-lg bg-blue-600 px-4 py-2 text-sm text-white hover:bg-blue-700" @click="joinRoom(room.id)">
              {{ TXT.join }}
            </button>
            <button
              v-if="authStore.isTeacher"
              class="rounded-lg border border-amber-300 px-4 py-2 text-sm text-amber-700 hover:bg-amber-50 disabled:opacity-50"
              :disabled="timerStartingRoomId === room.id"
              @click="handleTimerAction(room)"
            >
              {{ timerStartingRoomId === room.id ? TXT.startingTimer : timerActionText(room) }}
            </button>
            <button
              v-if="authStore.isTeacher"
              class="rounded-lg border border-red-300 px-4 py-2 text-sm text-red-600 hover:bg-red-50"
              @click="openDeleteModal(room)"
            >
              {{ TXT.delete }}
            </button>
          </div>
        </div>
      </div>
    </main>

    <div v-if="showModal" class="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div class="w-full max-w-xl rounded-xl bg-white p-6 shadow-xl">
        <h2 class="mb-4 text-lg font-bold">{{ TXT.createRoom }}</h2>
        <form class="space-y-4" @submit.prevent="handleCreateRoom">
          <div>
            <label class="mb-1 block text-sm font-medium text-gray-700">{{ TXT.roomName }}</label>
            <input
              v-model="newRoomName"
              type="text"
              required
              class="w-full rounded-lg border border-gray-300 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div>
            <label class="mb-1 block text-sm font-medium text-gray-700">{{ TXT.createMode }}</label>
            <div class="flex gap-3 rounded-lg border border-gray-200 p-2">
              <label class="inline-flex items-center gap-2 text-sm text-gray-700">
                <input v-model="createMode" type="radio" value="template" />
                {{ TXT.useTemplate }}
              </label>
              <label class="inline-flex items-center gap-2 text-sm text-gray-700">
                <input v-model="createMode" type="radio" value="manual" />
                {{ TXT.manualInput }}
              </label>
            </div>
          </div>
          <div>
            <label class="mb-1 block text-sm font-medium text-gray-700">{{ TXT.agentModeLabel }}</label>
            <select
              v-model="agentMode"
              class="w-full rounded-lg border border-gray-300 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="none">{{ TXT.agentModeNone }}</option>
              <option value="single">{{ TXT.agentModeSingle }}</option>
              <option value="multi">{{ TXT.agentModeMulti }}</option>
            </select>
          </div>

          <div v-if="createMode === 'template'">
            <label class="mb-1 block text-sm font-medium text-gray-700">{{ TXT.selectTemplate }}</label>
            <select
              v-model="selectedTemplateId"
              class="w-full rounded-lg border border-gray-300 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="" disabled>{{ templatesLoading ? TXT.loadingTemplates : TXT.selectTemplatePlaceholder }}</option>
              <option v-for="item in templates" :key="item.id" :value="item.id">
                {{ item.title || TXT.unnamedTemplate }}
              </option>
            </select>
            <p v-if="!templatesLoading && templates.length === 0" class="mt-1 text-xs text-amber-600">
              {{ TXT.noTemplatesHint }}
            </p>
          </div>

          <div v-if="createMode === 'template' && selectedTemplate">
            <label class="mb-1 block text-sm font-medium text-gray-700">{{ TXT.templatePreview }}</label>
            <textarea
              :value="selectedTemplate.requirements || TXT.noRequirements"
              rows="3"
              disabled
              class="w-full resize-none rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-gray-600"
            />
          </div>

          <div v-if="createMode === 'manual'">
            <label class="mb-1 block text-sm font-medium text-gray-700">{{ TXT.taskTitle }}</label>
            <input
              v-model="taskTitle"
              type="text"
              :placeholder="TXT.taskTitlePlaceholder"
              class="w-full rounded-lg border border-gray-300 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div v-if="createMode === 'manual'">
            <label class="mb-1 block text-sm font-medium text-gray-700">{{ TXT.taskRequirements }}</label>
            <textarea
              v-model="taskRequirements"
              rows="4"
              required
              :placeholder="TXT.taskRequirementsPlaceholder"
              class="w-full rounded-lg border border-gray-300 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <p v-if="createError" class="text-sm text-red-500">{{ createError }}</p>
          <div class="flex gap-3">
            <button type="button" class="flex-1 rounded-lg border border-gray-300 py-2 text-sm hover:bg-gray-50" @click="closeCreateModal">
              {{ TXT.cancel }}
            </button>
            <button type="submit" :disabled="creating" class="flex-1 rounded-lg bg-blue-600 py-2 text-sm text-white hover:bg-blue-700 disabled:opacity-50">
              {{ creating ? TXT.creating : TXT.confirmCreate }}
            </button>
          </div>
        </form>
      </div>
    </div>

    <div v-if="showDeleteModal" class="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div class="w-full max-w-md rounded-xl bg-white p-6 shadow-xl">
        <h2 class="mb-3 text-lg font-bold text-gray-800">{{ TXT.deleteRoomConfirm }}</h2>
        <p class="mb-2 text-sm text-gray-600">
          {{ TXT.deleteRoomInputHint }} <span class="font-semibold text-gray-900">{{ deletingRoom?.name }}</span>
        </p>
        <div class="mb-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
          {{ TXT.deleteRoomDataWarning }}
        </div>
        <input
          v-model="deleteConfirmName"
          type="text"
          class="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-400"
          :placeholder="TXT.inputRoomName"
        />
        <p v-if="deleteError" class="mt-2 text-sm text-red-500">{{ deleteError }}</p>
        <div class="mt-4 flex gap-3">
          <button type="button" class="flex-1 rounded-lg border border-gray-300 py-2 text-sm hover:bg-gray-50" @click="closeDeleteModal">
            {{ TXT.cancel }}
          </button>
          <button type="button" :disabled="!canDelete || deleting" class="flex-1 rounded-lg bg-red-600 py-2 text-sm text-white hover:bg-red-700 disabled:opacity-50" @click="confirmDeleteRoom">
            {{ deleting ? TXT.deleting : TXT.confirmDelete }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="showTemplateModal" class="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div class="w-full max-w-xl rounded-xl bg-white p-6 shadow-xl">
        <h2 class="mb-4 text-lg font-bold">{{ TXT.createTemplate }}</h2>
        <form class="space-y-4" @submit.prevent="handleCreateTemplate">
          <div>
            <label class="mb-1 block text-sm font-medium text-gray-700">{{ TXT.templateTitle }}</label>
            <input
              v-model="templateTitle"
              type="text"
              required
              class="w-full rounded-lg border border-gray-300 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
              :placeholder="TXT.templateTitlePlaceholder"
            />
          </div>
          <div>
            <label class="mb-1 block text-sm font-medium text-gray-700">{{ TXT.taskRequirements }}</label>
            <textarea
              v-model="templateRequirements"
              rows="4"
              required
              class="w-full rounded-lg border border-gray-300 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
              :placeholder="TXT.taskRequirementsPlaceholder"
            />
          </div>
          <p v-if="templateError" class="text-sm text-red-500">{{ templateError }}</p>
          <div class="flex gap-3">
            <button type="button" class="flex-1 rounded-lg border border-gray-300 py-2 text-sm hover:bg-gray-50" @click="closeTemplateModal">
              {{ TXT.cancel }}
            </button>
            <button type="submit" :disabled="templateCreating" class="flex-1 rounded-lg bg-blue-600 py-2 text-sm text-white hover:bg-blue-700 disabled:opacity-50">
              {{ templateCreating ? TXT.saving : TXT.saveTemplate }}
            </button>
          </div>
        </form>
      </div>
    </div>

    <div v-if="showTemplateManagerModal" class="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div class="w-full max-w-5xl rounded-xl bg-white p-6 shadow-xl">
        <div class="mb-4 flex items-center justify-between">
          <h2 class="text-lg font-bold">{{ TXT.manageTemplates }}</h2>
          <button class="rounded border border-gray-300 px-3 py-1 text-sm text-gray-600 hover:bg-gray-50" @click="closeTemplateManagerModal">
            {{ TXT.close }}
          </button>
        </div>

        <div class="grid grid-cols-1 gap-4 md:grid-cols-[280px_1fr]">
          <div class="rounded-lg border border-gray-200 p-3">
            <p class="mb-2 text-xs font-semibold text-gray-500">{{ TXT.templateList }}</p>
            <div v-if="templateManagerLoading" class="py-8 text-center text-sm text-gray-400">{{ TXT.loadingTemplates }}</div>
            <div v-else-if="templates.length === 0" class="py-8 text-center text-sm text-gray-400">{{ TXT.noTemplates }}</div>
            <div v-else class="max-h-[420px] space-y-2 overflow-auto pr-1">
              <button
                v-for="item in templates"
                :key="item.id"
                class="w-full rounded border px-3 py-2 text-left text-sm"
                :class="editingTemplateId === item.id ? 'border-blue-300 bg-blue-50 text-blue-800' : 'border-gray-200 bg-white text-gray-700 hover:bg-gray-50'"
                @click="selectTemplateForEdit(item)"
              >
                <div class="font-medium">{{ item.title || TXT.unnamedTemplate }}</div>
              </button>
            </div>
          </div>

          <div class="rounded-lg border border-gray-200 p-4">
            <div v-if="!editingTemplateId" class="py-16 text-center text-sm text-gray-400">{{ TXT.selectTemplateToEdit }}</div>
            <form v-else class="space-y-3" @submit.prevent="saveTemplateChanges">
              <div>
                <label class="mb-1 block text-sm font-medium text-gray-700">{{ TXT.templateTitle }}</label>
                <input
                  v-model="managerTemplateTitle"
                  type="text"
                  class="w-full rounded border border-gray-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                  required
                />
              </div>
              <div>
                <label class="mb-1 block text-sm font-medium text-gray-700">{{ TXT.taskRequirements }}</label>
                <textarea
                  v-model="managerTemplateRequirements"
                  rows="8"
                  class="w-full rounded border border-gray-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                  required
                />
              </div>
              <p v-if="templateManagerError" class="text-sm text-red-500">{{ templateManagerError }}</p>
              <div class="flex flex-wrap justify-end gap-2">
                <button
                  type="button"
                  class="rounded border border-red-300 px-3 py-2 text-sm text-red-600 hover:bg-red-50 disabled:opacity-50"
                  :disabled="templateDeleting"
                  @click="deleteEditingTemplate"
                >
                  {{ templateDeleting ? TXT.deleting : TXT.deleteTemplate }}
                </button>
                <button
                  type="submit"
                  class="rounded bg-blue-600 px-4 py-2 text-sm text-white hover:bg-blue-700 disabled:opacity-50"
                  :disabled="templateSaving"
                >
                  {{ templateSaving ? TXT.saving : TXT.saveChanges }}
                </button>
              </div>
            </form>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth.js'
import api from '../services/api.js'

const TXT = {
  lobbyTitle: '\u534f\u4f5c\u5b66\u4e60\u5e73\u53f0 - \u5927\u5385',
  teacher: '\u6559\u5e08',
  student: '\u5b66\u751f',
  logout: '\u9000\u51fa',
  createTemplate: '\u521b\u5efa\u6a21\u677f',
  manageTemplates: '\u7ba1\u7406\u6a21\u677f',
  exportData: '\u5b9e\u9a8c\u6570\u636e\u5bfc\u51fa',
  createRoom: '\u521b\u5efa\u623f\u95f4',
  noRooms: '\u6682\u65e0\u623f\u95f4',
  loadingRooms: '\u623f\u95f4\u52a0\u8f7d\u4e2d...',
  searchRoomPlaceholder: '\u68c0\u7d22\u623f\u95f4\u540d\u79f0...',
  filterAll: '\u5168\u90e8\u72b6\u6001',
  statusNotStarted: '\u672a\u5f00\u59cb',
  statusRunning: '\u8fdb\u884c\u4e2d',
  statusEnded: '\u5df2\u7ed3\u675f',
  members: '\u4eba',
  onlineMembers: '\u5728\u7ebf',
  join: '\u52a0\u5165',
  delete: '\u5220\u9664',
  startTimer: '\u5f00\u59cb\u8ba1\u65f6',
  resetTimer: '\u91cd\u7f6e\u8ba1\u65f6',
  startingTimer: '\u5904\u7406\u4e2d...',
  startTimerConfirm: '\u786e\u8ba4\u5f00\u59cb\u8be5\u623f\u95f4\u7684120\u5206\u949f\u8ba1\u65f6\uff1f',
  resetTimerConfirm: '\u786e\u8ba4\u91cd\u7f6e\u8be5\u623f\u95f4\u8ba1\u65f6\uff1f\u91cd\u7f6e\u540e\u5c06\u56de\u5230\u201c\u672a\u5f00\u59cb\u201d\u3002',
  startTimerNeedThreeStudents: '\u5f53\u524d\u5b66\u751f\u4eba\u6570\u4e0d\u8db33\u4eba\uff0c\u65e0\u6cd5\u5f00\u59cb\u8ba1\u65f6',
  timerActionFailed: '\u64cd\u4f5c\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5',
  roomName: '\u623f\u95f4\u540d\u79f0',
  createMode: '\u521b\u5efa\u65b9\u5f0f',
  useTemplate: '\u4f7f\u7528\u6a21\u677f',
  manualInput: '\u624b\u52a8\u8f93\u5165',
  agentModeLabel: '\u5b9e\u9a8c\u6761\u4ef6',
  agentModeNone: '\u65e0\u667a\u80fd\u4f53\u7ec4\uff1a\u4fdd\u7559\u534f\u4f5c\u5e73\u53f0\u57fa\u7840\u529f\u80fd\uff0c\u4e0d\u542f\u7528\u667a\u80fd\u4f53',
  agentModeSingle: '\u5355\u667a\u80fd\u4f53\u7ec4\uff1a\u4ec5\u542f\u7528\u901a\u7528AI\u52a9\u624b',
  agentModeMulti: '\u591a\u667a\u80fd\u4f53\u7ec4\uff1a\u542f\u7528\u591a\u89d2\u8272\u667a\u80fd\u4f53',
  selectTemplate: '\u9009\u62e9\u6a21\u677f',
  loadingTemplates: '\u6a21\u677f\u52a0\u8f7d\u4e2d...',
  selectTemplatePlaceholder: '\u8bf7\u9009\u62e9\u6a21\u677f',
  unnamedTemplate: '\u672a\u547d\u540d\u6a21\u677f',
  noTemplatesHint: '\u6682\u65e0\u53ef\u7528\u6a21\u677f\uff0c\u8bf7\u5207\u6362\u5230\u201c\u624b\u52a8\u8f93\u5165\u201d',
  templatePreview: '\u6a21\u677f\u9884\u89c8\uff1a\u4efb\u52a1\u8981\u6c42',
  noRequirements: '\u6682\u65e0\u4efb\u52a1\u8981\u6c42',
  taskTitle: '\u4efb\u52a1\u6807\u9898',
  taskTitlePlaceholder: '\u9ed8\u8ba4\u4f7f\u7528\u623f\u95f4\u540d',
  taskRequirements: '\u4efb\u52a1\u8981\u6c42',
  taskRequirementsPlaceholder: '\u8bf7\u8f93\u5165\u4efb\u52a1\u8981\u6c42...',
  cancel: '\u53d6\u6d88',
  creating: '\u521b\u5efa\u4e2d...',
  confirmCreate: '\u786e\u8ba4\u521b\u5efa',
  deleteRoomConfirm: '\u5220\u9664\u623f\u95f4\u786e\u8ba4',
  deleteRoomInputHint: '\u8bf7\u8f93\u5165\u623f\u95f4\u540d\u79f0\u4ee5\u786e\u8ba4\u5220\u9664\uff1a',
  deleteRoomDataWarning:
    '\u8b66\u544a\uff1a\u5220\u9664\u540e\u5c06\u6c38\u4e45\u6e05\u9664\u8be5\u623f\u95f4\u7684\u804a\u5929\u8bb0\u5f55\u3001\u6210\u5458\u5173\u7cfb\u548c\u8fd0\u884c\u6570\u636e\uff1b\u4f46\u4efb\u52a1\u6a21\u677f\u4ecd\u4f1a\u4fdd\u7559\u3002',
  inputRoomName: '\u8f93\u5165\u623f\u95f4\u540d\u79f0',
  deleting: '\u5220\u9664\u4e2d...',
  confirmDelete: '\u786e\u8ba4\u5220\u9664',
  templateTitle: '\u6a21\u677f\u6807\u9898',
  templateTitlePlaceholder: '\u8bf7\u8f93\u5165\u6a21\u677f\u6807\u9898',
  saving: '\u4fdd\u5b58\u4e2d...',
  saveTemplate: '\u4fdd\u5b58\u6a21\u677f',
  close: '\u5173\u95ed',
  templateList: '\u6a21\u677f\u5217\u8868',
  noTemplates: '\u6682\u65e0\u6a21\u677f',
  selectTemplateToEdit: '\u8bf7\u5148\u9009\u62e9\u5de6\u4fa7\u6a21\u677f\u8fdb\u884c\u7f16\u8f91',
  deleteTemplate: '\u5220\u9664\u6a21\u677f',
  saveChanges: '\u4fdd\u5b58\u4fee\u6539',
}

const router = useRouter()
const authStore = useAuthStore()
const ROOMS_CACHE_KEY = 'lobby:rooms:last'

const rooms = ref([])
const roomsLoading = ref(false)
const roomsError = ref('')
const roomSearchQuery = ref('')
const roomStatusFilter = ref('all')
const showModal = ref(false)
const newRoomName = ref('')
const createMode = ref('template')
const agentMode = ref('multi')
const templates = ref([])
const templatesLoading = ref(false)
const selectedTemplateId = ref('')
const taskTitle = ref('')
const taskRequirements = ref('')
const creating = ref(false)
const createError = ref('')

const showDeleteModal = ref(false)
const deletingRoom = ref(null)
const deleteConfirmName = ref('')
const deleteError = ref('')
const deleting = ref(false)

const showTemplateModal = ref(false)
const templateTitle = ref('')
const templateRequirements = ref('')
const templateCreating = ref(false)
const templateError = ref('')

const showTemplateManagerModal = ref(false)
const templateManagerLoading = ref(false)
const templateManagerError = ref('')
const templateSaving = ref(false)
const templateDeleting = ref(false)
const timerStartingRoomId = ref('')
const editingTemplateId = ref('')
const managerTemplateTitle = ref('')
const managerTemplateRequirements = ref('')
let roomsRequestId = 0

const canDelete = computed(() => {
  return !!deletingRoom.value && deleteConfirmName.value.trim() === deletingRoom.value.name
})
const selectedTemplate = computed(() => {
  if (!selectedTemplateId.value) return null
  return templates.value.find((t) => t.id === selectedTemplateId.value) || null
})
const filteredRooms = computed(() => {
  const keyword = roomSearchQuery.value.trim().toLowerCase()
  return rooms.value.filter((room) => {
    const roomName = String(room?.name || '').toLowerCase()
    const matchKeyword = !keyword || roomName.includes(keyword)
    const matchStatus = roomStatusFilter.value === 'all' || roomPhase(room) === roomStatusFilter.value
    return matchKeyword && matchStatus
  })
})

onMounted(async () => {
  loadRoomsCache()
  await fetchRooms()
})

function logout() {
  authStore.logout()
  router.push('/login')
}

function goTeacherExports() {
  router.push('/teacher/exports')
}

async function fetchRooms() {
  const requestId = ++roomsRequestId
  roomsLoading.value = true
  try {
    const data = await api.get('/rooms')
    if (requestId !== roomsRequestId) return
    rooms.value = Array.isArray(data) ? data : []
    roomsError.value = ''
    sessionStorage.setItem(ROOMS_CACHE_KEY, JSON.stringify(rooms.value))
  } catch (error) {
    if (requestId !== roomsRequestId) return
    roomsError.value = '房间列表刷新失败，请稍后重试'
    // Keep last successful rooms to avoid transient blank list.
  } finally {
    if (requestId === roomsRequestId) {
      roomsLoading.value = false
    }
  }
}

function loadRoomsCache() {
  try {
    const raw = sessionStorage.getItem(ROOMS_CACHE_KEY)
    if (!raw) return
    const parsed = JSON.parse(raw)
    if (Array.isArray(parsed)) {
      rooms.value = parsed
    }
  } catch {
    // ignore cache parse errors
  }
}

function roomPhase(room) {
  if (room?.timer_stopped_at) return 'ended'
  if (room?.timer_started_at) return 'running'
  return 'not_started'
}

function roomStatusText(room) {
  const phase = roomPhase(room)
  if (phase === 'ended') return TXT.statusEnded
  if (phase === 'running') return TXT.statusRunning
  return TXT.statusNotStarted
}

function roomStatusClass(room) {
  const phase = roomPhase(room)
  if (phase === 'ended') return 'bg-gray-100 text-gray-700'
  if (phase === 'running') return 'bg-green-100 text-green-700'
  return 'bg-yellow-100 text-yellow-700'
}

function timerActionText(room) {
  return roomPhase(room) === 'not_started' ? TXT.startTimer : TXT.resetTimer
}

async function handleTimerAction(room) {
  if (!room?.id || timerStartingRoomId.value) return
  const shouldStart = roomPhase(room) === 'not_started'
  const confirmed = window.confirm(shouldStart ? TXT.startTimerConfirm : TXT.resetTimerConfirm)
  if (!confirmed) return

  timerStartingRoomId.value = room.id
  try {
    const endpoint = shouldStart ? 'start' : 'reset'
    const updatedRoom = await api.post(`/rooms/${room.id}/timer/${endpoint}`)
    const idx = rooms.value.findIndex((item) => item.id === room.id)
    if (idx >= 0 && updatedRoom?.id) {
      rooms.value[idx] = { ...rooms.value[idx], ...updatedRoom }
    } else {
      await fetchRooms()
    }
  } catch (e) {
    const detail = e?.response?.data?.detail
    const message =
      (typeof detail === 'object' && detail?.message) ||
      (typeof detail === 'string' && detail) ||
      TXT.timerActionFailed
    alert(message)
  } finally {
    timerStartingRoomId.value = ''
  }
}

async function joinRoom(roomId) {
  await api.post(`/rooms/${roomId}/join`)
  router.push(`/room/${roomId}`)
}

function closeCreateModal() {
  showModal.value = false
  createError.value = ''
}

function openTemplateModal() {
  showTemplateModal.value = true
  templateError.value = ''
  templateTitle.value = ''
  templateRequirements.value = ''
}

function closeTemplateModal() {
  showTemplateModal.value = false
  templateError.value = ''
}

async function openCreateModal() {
  showModal.value = true
  createError.value = ''
  createMode.value = 'template'
  agentMode.value = 'multi'
  selectedTemplateId.value = ''
  await loadTemplates()
  if (!templates.value.length) {
    createMode.value = 'manual'
  }
}

async function loadTemplates() {
  templatesLoading.value = true
  try {
    const data = await api.get('/tasks')
    templates.value = Array.isArray(data) ? data : []
  } catch {
    templates.value = []
  } finally {
    templatesLoading.value = false
  }
}

async function handleCreateRoom() {
  createError.value = ''
  creating.value = true
  try {
    if (!newRoomName.value.trim()) {
      createError.value = 'Room name is required'
      return
    }

    let taskId = null
    if (createMode.value === 'template') {
      if (!selectedTemplate.value) {
        createError.value = 'Please select a template'
        return
      }
      taskId = selectedTemplate.value.id
    } else {
      if (!taskRequirements.value.trim()) {
        createError.value = 'Task requirements are required'
        return
      }

      const createdTask = await api.post('/tasks', {
        title: taskTitle.value.trim() || `${newRoomName.value.trim()} - Task`,
        requirements: taskRequirements.value.trim(),
      })
      taskId = createdTask.id
    }

    await api.post('/rooms', {
      name: newRoomName.value.trim(),
      task_id: taskId,
      agent_mode: agentMode.value || 'multi',
    })

    showModal.value = false
    newRoomName.value = ''
    agentMode.value = 'multi'
    selectedTemplateId.value = ''
    taskTitle.value = ''
    taskRequirements.value = ''
    await fetchRooms()
  } catch (e) {
    createError.value = e.response?.data?.detail || 'Create room failed'
  } finally {
    creating.value = false
  }
}

function openDeleteModal(room) {
  deletingRoom.value = room
  deleteConfirmName.value = ''
  deleteError.value = ''
  showDeleteModal.value = true
}

function closeDeleteModal() {
  showDeleteModal.value = false
  deletingRoom.value = null
  deleteConfirmName.value = ''
  deleteError.value = ''
}

async function confirmDeleteRoom() {
  if (!deletingRoom.value) return
  if (!canDelete.value) {
    deleteError.value = 'Please input exact room name'
    return
  }

  deleting.value = true
  deleteError.value = ''
  try {
    await api.delete(`/rooms/${deletingRoom.value.id}`, {
      data: {
        confirm_name: deleteConfirmName.value.trim(),
      },
    })
    closeDeleteModal()
    await fetchRooms()
  } catch (e) {
    deleteError.value = e.response?.data?.detail || 'Delete room failed'
  } finally {
    deleting.value = false
  }
}

async function handleCreateTemplate() {
  templateError.value = ''
  templateCreating.value = true
  try {
    if (!templateTitle.value.trim()) {
      templateError.value = 'Template title is required'
      return
    }
    if (!templateRequirements.value.trim()) {
      templateError.value = 'Task requirements are required'
      return
    }

    await api.post('/tasks', {
      title: templateTitle.value.trim(),
      requirements: templateRequirements.value.trim(),
    })

    await loadTemplates()
    closeTemplateModal()
  } catch (e) {
    templateError.value = e.response?.data?.detail || 'Create template failed'
  } finally {
    templateCreating.value = false
  }
}

async function openTemplateManagerModal() {
  showTemplateManagerModal.value = true
  templateManagerError.value = ''
  await refreshTemplateManager()
}

function closeTemplateManagerModal() {
  showTemplateManagerModal.value = false
  templateManagerError.value = ''
}

async function refreshTemplateManager() {
  templateManagerLoading.value = true
  templateManagerError.value = ''
  try {
    const data = await api.get('/tasks')
    templates.value = Array.isArray(data) ? data : []
    if (!templates.value.length) {
      editingTemplateId.value = ''
      managerTemplateTitle.value = ''
      managerTemplateRequirements.value = ''
      return
    }

    const existing = templates.value.find((t) => t.id === editingTemplateId.value)
    selectTemplateForEdit(existing || templates.value[0])
  } catch (e) {
    templateManagerError.value = e.response?.data?.detail || 'Load templates failed'
  } finally {
    templateManagerLoading.value = false
  }
}

function selectTemplateForEdit(item) {
  if (!item) return
  editingTemplateId.value = item.id
  managerTemplateTitle.value = item.title || ''
  managerTemplateRequirements.value = item.requirements || ''
  templateManagerError.value = ''
}

async function saveTemplateChanges() {
  if (!editingTemplateId.value) return
  templateManagerError.value = ''
  templateSaving.value = true
  try {
    if (!managerTemplateTitle.value.trim()) {
      templateManagerError.value = 'Template title is required'
      return
    }

    await api.patch(`/tasks/${editingTemplateId.value}`, {
      title: managerTemplateTitle.value.trim(),
      requirements: managerTemplateRequirements.value.trim() || null,
    })

    await refreshTemplateManager()
  } catch (e) {
    templateManagerError.value = e.response?.data?.detail || 'Save template failed'
  } finally {
    templateSaving.value = false
  }
}

async function deleteEditingTemplate() {
  if (!editingTemplateId.value || templateDeleting.value) return
  if (!window.confirm('Are you sure to delete this template?')) return

  templateDeleting.value = true
  templateManagerError.value = ''
  try {
    await api.delete(`/tasks/${editingTemplateId.value}`)
    await refreshTemplateManager()
  } catch (e) {
    templateManagerError.value = e.response?.data?.detail || 'Delete template failed'
  } finally {
    templateDeleting.value = false
  }
}
</script>

