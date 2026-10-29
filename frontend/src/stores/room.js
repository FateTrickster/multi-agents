import { defineStore } from 'pinia'
import api from '../services/api.js'
import { roomApi } from '../services/roomApi.js'

export const useRoomStore = defineStore('room', {
  state: () => ({
    currentRoomId: null,
    currentRoom: null,
    currentTask: null,
    loadingContext: false,
    contextError: '',
    savingTask: false,
    taskSaveError: '',
    taskSaveSuccessAt: 0,

    taskScriptState: null,
    loadingTaskScript: false,
    taskScriptError: '',
    requestingTaskScriptProposal: false,
    confirmingTaskScriptProposal: false,

    taskScriptLockState: null,
    loadingTaskScriptLock: false,
    taskScriptLockError: '',
    taskScriptLockNotice: '',
    acquiringTaskScriptLock: false,
    renewingTaskScriptLock: false,
    releasingTaskScriptLock: false,
    ownTaskScriptLeaseId: '',

    timerStartedAt: null,
    timerDeadlineAt: null,
    timerStoppedAt: null,
    startingRoomTimer: false,
    roomTimerError: '',

    writingSubmitState: null,
    loadingWritingSubmit: false,
    confirmingWritingSubmit: false,
    writingSubmitError: '',
  }),

  getters: {
    hasPendingTaskScriptProposal: (state) => !!state.taskScriptState?.pending_proposal,
    isTaskScriptLocked: (state) => !!state.taskScriptLockState?.locked,
    isTaskScriptLockMine: (state) => !!state.taskScriptLockState?.is_mine && !!state.ownTaskScriptLeaseId,
  },

  actions: {
    syncTimerFromRoom(room) {
      this.timerStartedAt = room?.timer_started_at || null
      this.timerDeadlineAt = room?.timer_deadline_at || null
      this.timerStoppedAt = room?.timer_stopped_at || null
    },

    applyRoomTimerUpdate(payload = {}) {
      const payloadRoomId = String(payload.room_id || '')
      if (!payloadRoomId || payloadRoomId !== this.currentRoomId) return

      this.timerStartedAt = payload.timer_started_at || null
      this.timerDeadlineAt = payload.timer_deadline_at || null
      this.timerStoppedAt = payload.timer_stopped_at || null
      if (this.currentRoom) {
        this.currentRoom = {
          ...this.currentRoom,
          timer_started_at: this.timerStartedAt,
          timer_deadline_at: this.timerDeadlineAt,
          timer_stopped_at: this.timerStoppedAt,
        }
      }
    },

    async loadRoomContext(roomId, force = false) {
      if (!roomId) return

      if (!force && this.currentRoomId === roomId && this.currentRoom) {
        return
      }

      this.loadingContext = true
      this.contextError = ''
      this.currentRoomId = roomId
      try {
        const room = await api.get(`/rooms/${roomId}`)
        this.currentRoom = room
        this.syncTimerFromRoom(room)
        this.currentTask = null

        if (room?.task_id) {
          this.currentTask = await api.get(`/tasks/${room.task_id}`)
        }
      } catch (error) {
        this.contextError = error.response?.data?.detail || 'Load room context failed'
        this.currentRoom = null
        this.currentTask = null
        this.taskScriptState = null
        this.taskScriptLockState = null
        this.syncTimerFromRoom(null)
      } finally {
        this.loadingContext = false
      }

      await this.loadTaskScriptState(roomId)
      await this.loadTaskScriptLockState(roomId)
      await this.loadWritingSubmitState(roomId)
    },

    async updateCurrentTask(patch) {
      this.savingTask = true
      this.taskSaveError = ''
      this.taskSaveSuccessAt = 0
      try {
        if (this.currentTask?.id) {
          const nextTask = await api.patch(`/tasks/${this.currentTask.id}`, patch)
          this.currentTask = nextTask
        } else {
          if (!this.currentRoomId || !this.currentRoom) {
            throw new Error('Current room context is missing, cannot bind task')
          }
          const createdTask = await api.post('/tasks', {
            title: 'Task',
            requirements: patch.requirements ?? null,
            scripts: patch.scripts ?? null,
          })

          const updatedRoom = await api.patch(`/rooms/${this.currentRoomId}/task`, {
            task_id: createdTask.id,
          })
          this.currentRoom = updatedRoom
          this.currentTask = createdTask
        }

        this.taskSaveSuccessAt = Date.now()
        if (this.currentRoomId) {
          await this.loadTaskScriptState(this.currentRoomId)
          await this.loadTaskScriptLockState(this.currentRoomId)
        }
      } catch (error) {
        this.taskSaveError = error.response?.data?.detail || error.message || 'Save task failed'
        throw error
      } finally {
        this.savingTask = false
      }
    },

    async startRoomTimer(roomId = this.currentRoomId) {
      if (!roomId) return null
      this.startingRoomTimer = true
      this.roomTimerError = ''
      try {
        const room = await roomApi.startRoomTimer(roomId)
        if (String(room?.id || '') === this.currentRoomId) {
          this.currentRoom = room
          this.syncTimerFromRoom(room)
        }
        return room
      } catch (error) {
        this.roomTimerError = error.response?.data?.detail || error.message || 'Timer action failed'
        throw error
      } finally {
        this.startingRoomTimer = false
      }
    },

    async resetRoomTimer(roomId = this.currentRoomId) {
      if (!roomId) return null
      this.startingRoomTimer = true
      this.roomTimerError = ''
      try {
        const room = await roomApi.resetRoomTimer(roomId)
        if (String(room?.id || '') === this.currentRoomId) {
          this.currentRoom = room
          this.syncTimerFromRoom(room)
        }
        return room
      } catch (error) {
        this.roomTimerError = error.response?.data?.detail || error.message || 'Timer action failed'
        throw error
      } finally {
        this.startingRoomTimer = false
      }
    },

    async loadTaskScriptState(roomId) {
      if (!roomId) return
      this.loadingTaskScript = true
      this.taskScriptError = ''
      try {
        this.taskScriptState = await roomApi.getTaskScriptState(roomId)
      } catch (error) {
        this.taskScriptState = null
        this.taskScriptError = error.response?.data?.detail || 'Load task script state failed'
      } finally {
        this.loadingTaskScript = false
      }
    },

    async loadTaskScriptLockState(roomId = this.currentRoomId) {
      if (!roomId) return
      this.loadingTaskScriptLock = true
      this.taskScriptLockError = ''
      try {
        this.taskScriptLockState = await roomApi.getTaskScriptLockState(roomId)
        if (!this.taskScriptLockState?.locked || this.taskScriptLockState?.is_mine) {
          this.taskScriptLockNotice = ''
        }
      } catch (error) {
        this.taskScriptLockError = error.response?.data?.detail || 'Load edit lock state failed'
      } finally {
        this.loadingTaskScriptLock = false
      }
    },

    async acquireTaskScriptLock() {
      if (!this.currentRoomId) return null
      this.acquiringTaskScriptLock = true
      this.taskScriptLockError = ''
      this.taskScriptLockNotice = ''
      try {
        const result = await roomApi.acquireTaskScriptLock(this.currentRoomId)
        this.taskScriptLockState = result.lock || null
        if (result.acquired && result.lease_id) {
          this.ownTaskScriptLeaseId = result.lease_id
          this.taskScriptLockNotice = ''
        }
        if (!result.acquired) {
          const ownerName = result?.lock?.owner_display_name || 'Another student'
          this.taskScriptLockNotice = `Currently edited by ${ownerName}. Your input is preserved, please try again later.`
        }
        return result
      } catch (error) {
        this.taskScriptLockError = error.response?.data?.detail || error.message || 'Acquire edit lock failed'
        return null
      } finally {
        this.acquiringTaskScriptLock = false
      }
    },

    async renewTaskScriptLock() {
      if (!this.currentRoomId || !this.ownTaskScriptLeaseId) return null
      this.renewingTaskScriptLock = true
      this.taskScriptLockError = ''
      try {
        const result = await roomApi.renewTaskScriptLock(this.currentRoomId, this.ownTaskScriptLeaseId)
        this.taskScriptLockState = result.lock || null
        this.taskScriptLockNotice = ''
        return result
      } catch (error) {
        this.ownTaskScriptLeaseId = ''
        this.taskScriptLockNotice = error.response?.data?.detail || 'Edit lock expired, please re-enter editing mode'
        return null
      } finally {
        this.renewingTaskScriptLock = false
      }
    },

    async releaseTaskScriptLock() {
      if (!this.currentRoomId || !this.ownTaskScriptLeaseId) return null
      this.releasingTaskScriptLock = true
      this.taskScriptLockError = ''
      try {
        const result = await roomApi.releaseTaskScriptLock(this.currentRoomId, this.ownTaskScriptLeaseId)
        this.taskScriptLockState = result.lock || null
        this.ownTaskScriptLeaseId = ''
        this.taskScriptLockNotice = ''
        return result
      } catch (error) {
        this.taskScriptLockError = error.response?.data?.detail || 'Release edit lock failed'
        return null
      } finally {
        this.releasingTaskScriptLock = false
      }
    },

    async requestFacilitatorTaskScriptProposal() {
      if (!this.currentRoomId) return
      this.requestingTaskScriptProposal = true
      this.taskScriptError = ''
      try {
        const nextState = await roomApi.requestFacilitatorProposal(this.currentRoomId)
        this.taskScriptState = nextState
        this.ownTaskScriptLeaseId = ''
        this.taskScriptLockNotice = ''
        await this.loadTaskScriptLockState(this.currentRoomId)
      } catch (error) {
        this.taskScriptError = error.response?.data?.detail || 'Generate facilitator proposal failed'
        throw error
      } finally {
        this.requestingTaskScriptProposal = false
      }
    },

    async confirmTaskScriptProposal(payload = {}) {
      if (!this.currentRoomId) return
      this.confirmingTaskScriptProposal = true
      this.taskScriptError = ''
      try {
        const nextState = await roomApi.confirmTaskScriptProposal(this.currentRoomId, payload)
        this.taskScriptState = nextState
        this.ownTaskScriptLeaseId = ''
        this.taskScriptLockNotice = ''
        await this.loadTaskScriptLockState(this.currentRoomId)
      } catch (error) {
        this.taskScriptError = error.response?.data?.detail || 'Confirm proposal failed'
        throw error
      } finally {
        this.confirmingTaskScriptProposal = false
      }
    },

    applyWritingSubmitUpdate(payload = {}) {
      const payloadRoomId = String(payload.room_id || '')
      if (!payloadRoomId || payloadRoomId !== this.currentRoomId) return
      const state = payload.state || {}
      this.writingSubmitState = {
        required_confirmations: Number(state.required_confirmations || 1),
        confirmations: Array.isArray(state.confirmations) ? state.confirmations : [],
        final_submitted_at: state.final_submitted_at || null,
        action: state.action || null,
      }
    },

    async loadWritingSubmitState(roomId = this.currentRoomId) {
      if (!roomId) return
      this.loadingWritingSubmit = true
      this.writingSubmitError = ''
      try {
        const state = await roomApi.getWritingSubmitState(roomId)
        this.writingSubmitState = {
          required_confirmations: Number(state.required_confirmations || 1),
          confirmations: Array.isArray(state.confirmations) ? state.confirmations : [],
          final_submitted_at: state.final_submitted_at || null,
          action: state.action || null,
        }
      } catch (error) {
        this.writingSubmitError = error.response?.data?.detail || 'Load writing submit state failed'
        this.writingSubmitState = {
          required_confirmations: 1,
          confirmations: [],
          final_submitted_at: null,
        }
      } finally {
        this.loadingWritingSubmit = false
      }
    },

    async confirmWritingSubmit(roomId = this.currentRoomId) {
      if (!roomId) return
      this.confirmingWritingSubmit = true
      this.writingSubmitError = ''
      try {
        const state = await roomApi.confirmWritingSubmit(roomId)
        this.writingSubmitState = {
          required_confirmations: Number(state.required_confirmations || 1),
          confirmations: Array.isArray(state.confirmations) ? state.confirmations : [],
          final_submitted_at: state.final_submitted_at || null,
          action: state.action || null,
        }
      } catch (error) {
        this.writingSubmitError = error.response?.data?.detail || 'Confirm writing submission failed'
        throw error
      } finally {
        this.confirmingWritingSubmit = false
      }
    },
  },
})
