<template>
  <div class="min-h-screen bg-gray-100">
    <header class="flex items-center justify-between bg-white px-6 py-4 shadow-sm">
      <h1 class="text-lg font-bold text-gray-800">实验数据导出</h1>
      <button class="rounded-lg border border-gray-300 px-4 py-2 text-sm hover:bg-gray-50" @click="goLobby">
        返回大厅
      </button>
    </header>

    <main class="mx-auto max-w-6xl space-y-6 px-6 py-8">
      <section class="rounded-xl border border-gray-200 bg-white p-5">
        <h2 class="mb-4 text-base font-semibold text-gray-800">筛选条件</h2>
        <div class="grid grid-cols-1 gap-4 md:grid-cols-3">
          <div>
            <label class="mb-1 block text-sm text-gray-700">房间名前缀</label>
            <input
              v-model="filters.room_name_prefix"
              type="text"
              class="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              placeholder="例如：实验班A"
            />
          </div>
          <div>
            <label class="mb-1 block text-sm text-gray-700">开始时间</label>
            <input
              v-model="filters.start_time"
              type="datetime-local"
              class="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div>
            <label class="mb-1 block text-sm text-gray-700">结束时间</label>
            <input
              v-model="filters.end_time"
              type="datetime-local"
              class="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
        </div>

        <div class="mt-4 flex flex-wrap gap-3">
          <button
            class="rounded-lg bg-blue-600 px-4 py-2 text-sm text-white hover:bg-blue-700 disabled:opacity-50"
            :disabled="loadingRooms"
            @click="loadRooms"
          >
            {{ loadingRooms ? '加载中...' : '加载房间' }}
          </button>
          <button
            class="rounded-lg border border-indigo-300 px-4 py-2 text-sm text-indigo-700 hover:bg-indigo-50 disabled:opacity-50"
            :disabled="previewLoading"
            @click="runPreview"
          >
            {{ previewLoading ? '预览中...' : '预览数据量' }}
          </button>
          <button
            class="rounded-lg border border-green-300 px-4 py-2 text-sm text-green-700 hover:bg-green-50 disabled:opacity-50"
            :disabled="downloadLoading"
            @click="runDownload"
          >
            {{ downloadLoading ? '导出中...' : '导出 ZIP' }}
          </button>
        </div>

        <p v-if="errorMessage" class="mt-3 text-sm text-red-600">{{ errorMessage }}</p>
        <p v-if="successMessage" class="mt-3 text-sm text-green-700">{{ successMessage }}</p>
      </section>

      <section class="rounded-xl border border-gray-200 bg-white p-5">
        <h2 class="mb-3 text-base font-semibold text-gray-800">房间列表（多选）</h2>
        <div v-if="rooms.length === 0" class="text-sm text-gray-500">暂无房间，请先点击“加载房间”。</div>
        <div v-else class="max-h-72 space-y-2 overflow-auto rounded border border-gray-200 p-3">
          <label v-for="room in rooms" :key="room.room_id" class="flex items-center gap-3 rounded px-2 py-1 hover:bg-gray-50">
            <input v-model="selectedRoomIds" type="checkbox" :value="room.room_id" />
            <span class="text-sm text-gray-800">{{ room.name || room.room_id }}</span>
            <span class="text-xs text-gray-500">{{ room.room_id }}</span>
          </label>
        </div>
      </section>

      <section class="rounded-xl border border-gray-200 bg-white p-5">
        <h2 class="mb-3 text-base font-semibold text-gray-800">导出内容</h2>
        <div class="grid grid-cols-1 gap-2 md:grid-cols-2">
          <label v-for="key in includeKeys" :key="key" class="flex items-center gap-2 text-sm text-gray-700">
            <input v-model="include[key]" type="checkbox" />
            {{ key === 'writing_snapshots' ? '写作区每5分钟历史快照' : key }}
          </label>
        </div>
      </section>

      <section class="rounded-xl border border-gray-200 bg-white p-5">
        <h2 class="mb-3 text-base font-semibold text-gray-800">预览结果</h2>
        <div v-if="!previewResult" class="text-sm text-gray-500">尚未执行预览。</div>
        <div v-else class="space-y-2 text-sm text-gray-700">
          <p>matched room count: {{ previewResult.matched_room_ids?.length || 0 }}</p>
          <p>rooms: {{ previewResult.counts?.rooms || 0 }}</p>
          <p>room_members: {{ previewResult.counts?.room_members || 0 }}</p>
          <p>messages: {{ previewResult.counts?.messages || 0 }}</p>
          <p>agent_tasks: {{ previewResult.counts?.agent_tasks || 0 }}</p>
          <p>agent_mention_requests: {{ previewResult.counts?.agent_mention_requests || 0 }}</p>
          <p>agent_task_events: {{ previewResult.counts?.agent_task_events || 0 }}</p>
          <p>analysis_snapshots: {{ previewResult.counts?.analysis_snapshots || 0 }}</p>
          <p>writing_docs: {{ previewResult.counts?.writing_docs || 0 }}</p>
          <p>writing_change_logs: {{ previewResult.counts?.writing_change_logs || 0 }}</p>
          <p>writing_snapshots (5 min): {{ previewResult.counts?.writing_snapshots || 0 }}</p>
          <p>task_definitions: {{ previewResult.counts?.task_definitions || 0 }}</p>
          <p>task_flow_states: {{ previewResult.counts?.task_flow_states || 0 }}</p>
          <p>writing_submission_states: {{ previewResult.counts?.writing_submission_states || 0 }}</p>
          <div>
            <p class="font-medium text-gray-800">estimated_files:</p>
            <ul class="ml-5 list-disc">
              <li v-for="name in previewResult.estimated_files || []" :key="name">{{ name }}</li>
            </ul>
          </div>
        </div>
      </section>
    </main>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { downloadExport, getExportRooms, previewExport } from '../services/exportApi.js'

const router = useRouter()
const includeKeys = [
  'rooms',
  'room_members',
  'messages',
  'agent_tasks',
  'agent_mention_requests',
  'agent_task_events',
  'analysis_snapshots',
  'writing_docs',
  'writing_change_logs',
  'writing_snapshots',
  'task_definitions',
  'task_flow_states',
  'writing_submission_states',
  'room_writing_html',
]

const filters = reactive({
  room_name_prefix: '',
  start_time: '',
  end_time: '',
})

const include = reactive({
  rooms: true,
  room_members: true,
  messages: true,
  agent_tasks: true,
  agent_mention_requests: true,
  agent_task_events: true,
  analysis_snapshots: true,
  writing_docs: true,
  writing_change_logs: true,
  writing_snapshots: true,
  task_definitions: true,
  task_flow_states: true,
  writing_submission_states: true,
  room_writing_html: true,
})

const rooms = ref([])
const selectedRoomIds = ref([])
const previewResult = ref(null)
const loadingRooms = ref(false)
const previewLoading = ref(false)
const downloadLoading = ref(false)
const errorMessage = ref('')
const successMessage = ref('')

function goLobby() {
  router.push('/lobby')
}

function normalizeTime(value) {
  if (!value) return null
  return new Date(value).toISOString()
}

function buildPayload() {
  const payload = {
    include: { ...include },
  }

  if (filters.room_name_prefix.trim()) {
    payload.room_name_prefix = filters.room_name_prefix.trim()
  }
  const startTime = normalizeTime(filters.start_time)
  const endTime = normalizeTime(filters.end_time)
  if (startTime) payload.start_time = startTime
  if (endTime) payload.end_time = endTime

  if (selectedRoomIds.value.length > 0) {
    payload.room_ids = [...selectedRoomIds.value]
  }
  return payload
}

async function loadRooms() {
  loadingRooms.value = true
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const params = {}
    if (filters.room_name_prefix.trim()) params.room_name_prefix = filters.room_name_prefix.trim()
    const startTime = normalizeTime(filters.start_time)
    const endTime = normalizeTime(filters.end_time)
    if (startTime) params.start_time = startTime
    if (endTime) params.end_time = endTime
    params.limit = 200
    rooms.value = await getExportRooms(params)
  } catch (error) {
    errorMessage.value = error?.response?.data?.detail || error?.message || '加载房间失败'
  } finally {
    loadingRooms.value = false
  }
}

async function runPreview() {
  previewLoading.value = true
  errorMessage.value = ''
  successMessage.value = ''
  try {
    previewResult.value = await previewExport(buildPayload())
    successMessage.value = '预览完成'
  } catch (error) {
    errorMessage.value = error?.response?.data?.detail || error?.message || '预览失败'
  } finally {
    previewLoading.value = false
  }
}

async function runDownload() {
  downloadLoading.value = true
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const result = await downloadExport(buildPayload())
    successMessage.value = `导出成功：${result.filename}`
  } catch (error) {
    errorMessage.value = error?.response?.data?.detail || error?.message || '导出失败'
  } finally {
    downloadLoading.value = false
  }
}
</script>

