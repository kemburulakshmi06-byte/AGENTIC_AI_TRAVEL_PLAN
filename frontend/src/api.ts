import type {
  TripCreatedResponse,
  TripDetails,
  TripListResponse,
  TripPlanRequest,
  TripStatusResponse,
} from './types'

const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000'
).replace(/\/+$/, '')
const REQUEST_TIMEOUT_MS = 12_000

export class ApiError extends Error {
  readonly status?: number
  readonly validationDetails?: string[]

  constructor(
    message: string,
    status?: number,
    validationDetails?: string[],
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.validationDetails = validationDetails
  }
}

async function requestJson<T>(
  path: string,
  init: RequestInit = {},
  timeoutMs = REQUEST_TIMEOUT_MS,
): Promise<T> {
  const controller = new AbortController()
  const externalSignal = init.signal
  const abortRequest = () => controller.abort()
  externalSignal?.addEventListener('abort', abortRequest, { once: true })
  if (externalSignal?.aborted) controller.abort()
  const timeout = globalThis.setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      signal: controller.signal,
      headers: {
        Accept: 'application/json',
        ...(init.body ? { 'Content-Type': 'application/json' } : {}),
        ...init.headers,
      },
    })
    if (!response.ok) {
      const payload: unknown = await response.json().catch(() => null)
      throw toApiError(response.status, payload)
    }
    return (await response.json()) as T
  } catch (error) {
    if (error instanceof ApiError) throw error
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new ApiError('The server took too long to respond.')
    }
    throw new ApiError(
      'The travel service is unavailable. Check your connection and try again.',
    )
  } finally {
    globalThis.clearTimeout(timeout)
    externalSignal?.removeEventListener('abort', abortRequest)
  }
}

function toApiError(status: number, payload: unknown): ApiError {
  if (status === 422 && isRecord(payload)) {
    const detail = payload.detail
    const validationDetails = Array.isArray(detail)
      ? detail
          .map((item) =>
            isRecord(item) && typeof item.msg === 'string'
              ? item.msg
              : undefined,
          )
          .filter((item): item is string => Boolean(item))
      : []
    return new ApiError(
      validationDetails[0] || 'Please check the trip details and try again.',
      status,
      validationDetails,
    )
  }
  if ((status === 403 || status === 409) && isRecord(payload) && isRecord(payload.detail)
    && typeof payload.detail.message === 'string') {
    return new ApiError(payload.detail.message, status)
  }
  if (status === 404) {
    return new ApiError('This trip could not be found.', status)
  }
  if (status >= 500) {
    return new ApiError(
      'The travel service could not complete that request. Please try again later.',
      status,
    )
  }
  return new ApiError('The request could not be completed.', status)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

export function planTrip(
  payload: TripPlanRequest,
): Promise<TripCreatedResponse> {
  return requestJson('/api/v1/trips/plan', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function getTripStatus(
  tripId: string,
  signal?: AbortSignal,
): Promise<TripStatusResponse> {
  return requestJson(
    `/api/v1/trips/${encodeURIComponent(tripId)}/status`,
    { signal },
  )
}

export function getTrip(
  tripId: string,
  signal?: AbortSignal,
): Promise<TripDetails> {
  return requestJson(`/api/v1/trips/${encodeURIComponent(tripId)}`, {
    signal,
  })
}

export function getSavedTrips(
  signal?: AbortSignal,
): Promise<TripListResponse> {
  return requestJson('/api/v1/trips?limit=8&offset=0', { signal })
}

export function reviseTrip(tripId: string, allowEconomyAccommodation = false): Promise<TripCreatedResponse> {
  return requestJson(`/api/v1/trips/${encodeURIComponent(tripId)}/revise`, {
    method: 'POST',
    headers: { 'X-Trip-Revision': '1' },
    body: JSON.stringify({ allow_economy_accommodation: allowEconomyAccommodation }),
  })
}
