<template>
  <div class="v2-auth min-h-screen flex items-center justify-center px-4 py-12">
    <div class="v2-auth-card w-full max-w-md p-8 sm:p-10">
      <h1 class="text-2xl font-bold text-center mb-6 text-gray-800">协作学习工作台</h1>

      <!-- Tab 切换 -->
      <div class="flex border-b mb-6">
        <button
          class="flex-1 py-2 text-sm font-medium"
          :class="tab === 'login' ? 'border-b-2 border-blue-500 text-blue-600' : 'text-gray-500'"
          @click="tab = 'login'"
        >
          登录
        </button>
        <button
          class="flex-1 py-2 text-sm font-medium"
          :class="tab === 'register' ? 'border-b-2 border-blue-500 text-blue-600' : 'text-gray-500'"
          @click="tab = 'register'"
        >
          注册
        </button>
      </div>

      <!-- 登录表单 -->
      <form v-if="tab === 'login'" @submit.prevent="handleLogin" class="space-y-4">
        <div>
          <label class="block text-sm font-medium text-gray-700 mb-1">用户名</label>
          <input v-model="loginForm.username" type="text" required
            class="w-full border border-gray-300 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500" />
        </div>
        <div>
          <label class="block text-sm font-medium text-gray-700 mb-1">密码</label>
          <input v-model="loginForm.password" type="password" required minlength="6"
            class="w-full border border-gray-300 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500" />
        </div>
        <p v-if="error" class="text-red-500 text-sm">{{ error }}</p>
        <button type="submit" :disabled="loading"
          class="w-full bg-blue-600 text-white py-2 rounded-lg hover:bg-blue-700 disabled:opacity-50">
          {{ loading ? '登录中...' : '登录' }}
        </button>
      </form>

      <!-- 注册表单 -->
      <form v-else @submit.prevent="handleRegister" class="space-y-4">
        <div>
          <label class="block text-sm font-medium text-gray-700 mb-1">用户名</label>
          <input v-model="registerForm.username" type="text" required minlength="3" maxlength="50"
            class="w-full border border-gray-300 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500" />
        </div>
        <div>
          <label class="block text-sm font-medium text-gray-700 mb-1">显示名称</label>
          <input v-model="registerForm.display_name" type="text" required maxlength="100"
            class="w-full border border-gray-300 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500" />
        </div>
        <div>
          <label class="block text-sm font-medium text-gray-700 mb-1">密码</label>
          <input v-model="registerForm.password" type="password" required minlength="6"
            class="w-full border border-gray-300 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500" />
        </div>
        <p class="text-sm text-gray-600">公开注册仅创建学生账号；教师账号请联系平台管理员。</p>
        <p v-if="error" class="text-red-500 text-sm">{{ error }}</p>
        <button type="submit" :disabled="loading"
          class="w-full bg-blue-600 text-white py-2 rounded-lg hover:bg-blue-700 disabled:opacity-50">
          {{ loading ? '注册中...' : '注册' }}
        </button>
      </form>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth.js'

const router = useRouter()
const authStore = useAuthStore()

const tab = ref('login')
const loading = ref(false)
const error = ref('')

const loginForm = ref({ username: '', password: '' })
const registerForm = ref({ username: '', display_name: '', password: '', role: 'student' })

async function handleLogin() {
  error.value = ''
  loading.value = true
  try {
    await authStore.login(loginForm.value.username, loginForm.value.password)
    redirectAfterAuth()
  } catch (e) {
    error.value = e.response?.data?.detail || '登录失败'
  } finally {
    loading.value = false
  }
}

async function handleRegister() {
  error.value = ''
  loading.value = true
  try {
    await authStore.register(registerForm.value)
    redirectAfterAuth()
  } catch (e) {
    error.value = e.response?.data?.detail || '注册失败'
  } finally {
    loading.value = false
  }
}

function redirectAfterAuth() {
  router.push('/lobby')
}
</script>
