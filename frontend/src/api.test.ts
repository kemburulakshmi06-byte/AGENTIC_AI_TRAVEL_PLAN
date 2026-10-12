import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, getSavedTrips, getTripStatus, planTrip } from './api'
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
