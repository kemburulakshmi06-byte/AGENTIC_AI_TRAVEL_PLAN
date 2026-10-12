import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { getSavedTrips, getTripStatus } from './api'
import App from './App'

vi.mock('./api', async (importOriginal) => ({
  ...await importOriginal<typeof import('./api')>(),
  getSavedTrips: vi.fn(),
  getTrip: vi.fn(),
  getTripStatus: vi.fn(),
  planTrip: vi.fn(),
}))

describe('travel planner landing page', () => {
  it('shows empty history as an empty journal', async () => {
    vi.mocked(getSavedTrips).mockResolvedValue({ items: [], total: 0, limit: 8, offset: 0 })
    render(<App />)
    expect(await screen.findByText('No saved trips to show just yet.')).toBeInTheDocument()
    expect(screen.queryByText(/trip listing is currently disabled/i)).not.toBeInTheDocument()
  })
  beforeEach(() => {
    sessionStorage.clear()
    Element.prototype.scrollIntoView = vi.fn()
    vi.mocked(getSavedTrips).mockRejectedValue(
      Object.assign(new Error('Trip listing disabled.'), { status: 404 }),
    )
  })

  it('restores the selected trip after refresh using fresh API status', async () => {
    const id = '57d5f15c-fb08-4c2c-b527-3c763a8b3a7b'
    sessionStorage.setItem('wayfare.activeTripId', id)
    vi.mocked(getTripStatus).mockRejectedValue(Object.assign(new Error('Unavailable'), { status: 404 }))
    const view = render(<App />)
    expect(await screen.findByText('Unavailable')).toBeInTheDocument()
    expect(getTripStatus).toHaveBeenCalledWith(id, expect.any(AbortSignal))
    view.unmount()
  })

  it('opens a historical trip and retains its ID for refresh', async () => {
    const id = '57d5f15c-fb08-4c2c-b527-3c763a8b3a7b'
    vi.mocked(getSavedTrips).mockResolvedValue({ items: [{ id, title: 'Saved journey', status: 'queued',
      origin: 'Origin', destination: 'Destination', start_date: '2030-01-01', end_date: '2030-01-02',
      travelers_count: 2, total_budget: '20000', currency: 'INR', total_estimated_cost: null }], total: 1, limit: 8, offset: 0 })
    vi.mocked(getTripStatus).mockRejectedValue(new Error('Temporarily unavailable'))
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: 'View trip' }))
    expect(sessionStorage.getItem('wayfare.activeTripId')).toBe(id)
    expect(await screen.findByText('Temporarily unavailable')).toBeInTheDocument()
  })

  it('ignores invalid saved IDs', () => {
    sessionStorage.setItem('wayfare.activeTripId', 'bad/id')
    render(<App />)
    expect(screen.getByRole('heading', { name: /plan a trip that feels like you/i })).toBeInTheDocument()
  })

  it('renders accessible planner and navigation structure', async () => {
    render(<App />)
    expect(
      screen.getByRole('navigation', { name: 'Main navigation' }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { name: /the world is wide/i }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { name: /plan a trip that feels like you/i }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('link', { name: /plan your trip/i }),
    ).toHaveAttribute('href', '#planner')
    expect(
      await screen.findByText(/trip listing is currently disabled/i),
    ).toBeInTheDocument()
  })
})
