import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../router/index.js', () => ({ default: { push: vi.fn() } }))
vi.mock('../stores/auth.js', () => ({ useAuthStore: vi.fn() }))

import api from './api.js'

describe('api', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('uses fetch while preserving the existing response and error contract', async () => {
    localStorage.setItem('token', 'token-1')
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: 1 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'bad request' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      }))

    await expect(api.get('/rooms', { params: { status: 'open' } })).resolves.toEqual({ id: 1 })
    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/rooms?status=open', expect.objectContaining({
      method: 'GET',
      headers: { Authorization: 'Bearer token-1' },
    }))
    await expect(api.post('/rooms', { name: 'demo' })).rejects.toMatchObject({
      response: { status: 400, data: { detail: 'bad request' } },
    })
  })
})
