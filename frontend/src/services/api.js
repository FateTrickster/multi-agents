import router from '../router/index.js'
import { useAuthStore } from '../stores/auth.js'

let sessionRevokedHandled = false

async function request(method, path, { data, params } = {}) {
  const token = localStorage.getItem('token')
  const query = new URLSearchParams(params || {}).toString()
  const response = await fetch(`/api${path}${query ? `?${query}` : ''}`, {
    method,
    headers: {
      ...(data === undefined ? {} : { 'Content-Type': 'application/json' }),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    ...(data === undefined ? {} : { body: JSON.stringify(data) }),
  })
  const contentType = response.headers.get('content-type') || ''
  const responseData = response.status === 204
    ? null
    : contentType.includes('application/json')
      ? await response.json()
      : await response.text()

  if (!response.ok) {
    if (response.status === 401) {
      const code = responseData?.detail?.code
      if (code === 'SESSION_REVOKED' && !sessionRevokedHandled) {
        sessionRevokedHandled = true
        try {
          const authStore = useAuthStore()
          authStore.handleSessionRevoked('你的账号已在其他设备登录，当前会话已失效')
        } catch {
          localStorage.removeItem('token')
          localStorage.removeItem('user')
          window.alert('你的账号已在其他设备登录，当前会话已失效')
        }
      } else {
        localStorage.removeItem('token')
        localStorage.removeItem('user')
      }
      router.push('/login')
    }

    const error = new Error(`Request failed with status ${response.status}`)
    error.response = { status: response.status, data: responseData }
    throw error
  }

  return responseData
}

const api = {
  get: (path, options) => request('GET', path, options),
  post: (path, data) => request('POST', path, { data }),
  patch: (path, data) => request('PATCH', path, { data }),
  delete: (path, options) => request('DELETE', path, options),
}

export default api
