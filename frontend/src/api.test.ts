import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, getSavedTrips, getTripStatus, planTrip, reviseTrip } from './api'
import type { TripPlanRequest } from './types'

const request: TripPlanRequest = {
  origin: 'Seattle',
  destination: 'Kyoto',
  start_date: '2030-05-01',
  end_date: '2030-05-06',
  travelers_count: 2,
  total_budget: '3500.00',
  currency: 'USD',
  interests: [],
  constraints: {},
}

afterEach(() => vi.unstubAllGlobals())

describe('trip API client', () => {
  it('surfaces the real request deadline without aborting the component lifecycle signal', async () => {
    vi.useFakeTimers()
    const lifecycle = new AbortController()
    const fetchMock = vi.fn().mockImplementation((_url, init: RequestInit) => new Promise((_resolve, reject) => {
      init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
    }))
    vi.stubGlobal('fetch', fetchMock)
    try {
      const outcome = expect(getTripStatus('trip-123', lifecycle.signal)).rejects.toMatchObject({
        name: 'ApiError', message: 'The server took too long to respond.',
      })
      await vi.advanceTimersByTimeAsync(12_000)
      await outcome
      expect(fetchMock).toHaveBeenCalledTimes(1)
      expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true)
      expect(lifecycle.signal.aborted).toBe(false)
      expect(vi.getTimerCount()).toBe(0)
    } finally { vi.useRealTimers() }
  })

  it('surfaces browser network/CORS fetch failures as errors instead of ignoring them', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    await expect(getTripStatus('trip-123')).rejects.toMatchObject({
      name: 'ApiError', message: 'The travel service is unavailable. Check your connection and try again.',
    })
  })

  it('uses backend port 8000 when no API URL override is supplied', async () => {
    vi.stubEnv('VITE_API_BASE_URL', '')
    vi.resetModules()
    try {
      const { planTrip: defaultPlanTrip } = await import('./api')
      const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
        trip_id: 'trip-123', task_id: 'trip-123', status: 'queued', message: 'Accepted',
      }), { status: 202 }))
      vi.stubGlobal('fetch', fetchMock)
      await defaultPlanTrip(request)
      expect(fetchMock).toHaveBeenCalledWith('http://127.0.0.1:8000/api/v1/trips/plan', expect.any(Object))
    } finally {
      vi.unstubAllEnvs()
      vi.resetModules()
    }
  })
  it('requests the existing paginated history endpoint and preserves empty results', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ items: [], total: 0, limit: 8, offset: 0 }),
      { status: 200 },
    ))
    vi.stubGlobal('fetch', fetchMock)
    await expect(getSavedTrips()).resolves.toEqual({ items: [], total: 0, limit: 8, offset: 0 })
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/v1\/trips\?limit=8&offset=0$/), expect.any(Object),
    )
  })

  it('reports status storage errors without treating them as planning success', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: { code: 'trip_status_unavailable', message: 'Unavailable' } }),
      { status: 503 },
    )))
    await expect(getTripStatus('trip-123')).rejects.toMatchObject({ name: 'ApiError', status: 503 })
  })
  it('posts the request and accepts the asynchronous 202 response', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          trip_id: 'trip-123',
          task_id: 'trip-123',
          status: 'queued',
          message: 'Accepted',
        }),
        { status: 202, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(planTrip(request)).resolves.toMatchObject({
      trip_id: 'trip-123',
      status: 'queued',
    })
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/v1\/trips\/plan$/),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify(request),
      }),
    )
  })

  it('returns sanitized validation errors from HTTP 422', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ detail: [{ msg: 'end_date must be after start_date' }] }),
          { status: 422, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    )
    await expect(planTrip(request)).rejects.toMatchObject({
      name: 'ApiError',
      status: 422,
      message: 'end_date must be after start_date',
    })
  })

  it('encodes trip IDs in status requests', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          trip_id: 'trip/a',
          task_id: 'trip/a',
          status: 'running',
          current_stage: 'itinerary',
          progress_percentage: 70,
          created_at: '2030-01-01T00:00:00Z',
          updated_at: '2030-01-01T00:00:00Z',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)
    await getTripStatus('trip/a')
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('trip%2Fa/status'),
      expect.any(Object),
    )
  })

  it('does not leak provider payloads in server errors', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: 'secret token value' }), {
          status: 500,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )
    let caught: unknown
    try {
      await planTrip(request)
    } catch (error) {
      caught = error
    }
    expect(caught).toBeInstanceOf(ApiError)
    expect((caught as Error).message).not.toContain('secret')
  })
})


describe('budget revision API', () => {
  it('posts only the explicit accommodation permission to the existing trip', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ trip_id: 'trip-123', task_id: 'trip-123', status: 'queued', message: 'Accepted' }), { status: 202 }))
    vi.stubGlobal('fetch', fetchMock)
    await reviseTrip('trip-123', true)
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('/api/v1/trips/trip-123/revise'), expect.objectContaining({
      method: 'POST', body: JSON.stringify({ allow_economy_accommodation: true }),
      headers: expect.objectContaining({ 'X-Trip-Revision': '1', 'Content-Type': 'application/json' }),
    }))
  })
  it.each([403, 409])('preserves the sanitized backend eligibility message for %s', async (status) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: { message: 'Revision not permitted.' } }), { status })))
    await expect(reviseTrip('trip-123')).rejects.toMatchObject({ status, message: 'Revision not permitted.' })
  })
})
