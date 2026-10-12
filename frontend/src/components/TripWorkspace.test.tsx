import { act, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ApiError, getTrip, getTripStatus } from '../api'
import type { TripDetails, TripStatusResponse } from '../types'
import { TripWorkspace } from './TripWorkspace'
import capturedPartialResponse from '../test-fixtures/real-partial-budget-20261012.json'

vi.mock('../api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../api')>(),
  getTrip: vi.fn(),
  getTripStatus: vi.fn(),
}))

const now = '2030-01-01T00:00:00Z'
const completedStatus: TripStatusResponse = {
  trip_id: 'trip-123',
  task_id: 'trip-123',
  status: 'completed',
  current_stage: 'optimization_review',
  progress_percentage: 100,
  message: null,
  error_message: null,
  created_at: now,
  updated_at: now,
}

const trip: TripDetails = {
  id: 'trip-123',
  title: 'Kyoto, slowly',
  status: 'completed',
  request: {
    origin: 'Seattle',
    destination: 'Kyoto',
    start_date: '2030-05-01',
    end_date: '2030-05-02',
    travelers_count: 2,
    total_budget: '3500.00',
    currency: 'USD',
  },
  total_estimated_cost: '1200.00',
  currency: 'USD',
  created_at: now,
  updated_at: now,
  itinerary: null,
  budget: null,
  optimization_review: { approved: true, review_status: 'APPROVED' },
  days: [
    {
      day_number: 1,
      itinerary_date: '2030-05-01',
      summary: 'A gentle first day',
      estimated_daily_cost: '75.00',
      activities: [
        {
          title: 'Visit the garden',
          description: 'A quiet morning among the trees.',
          location_name: 'Kyoto',
          start_time: '09:00:00',
          end_time: '10:30:00',
          estimated_cost: '12.00',
          category: 'culture',
          metadata: {},
        },
      ],
    },
  ],
  warnings: [],
  errors: [],
}

describe('TripWorkspace', () => {
  it('renders the captured real-provider replay budget and saved itinerary day', async () => {
    // Recorded isolated live run, 2026-10-12. Historical estimates, not quotes.
    // Fixture retains the first real day and category outputs; provider prose is trimmed.
    const captured = capturedPartialResponse as TripDetails
    vi.mocked(getTripStatus).mockResolvedValue({ ...completedStatus, trip_id: captured.id, task_id: captured.id, status: 'insufficient_data' })
    vi.mocked(getTrip).mockResolvedValue(captured)
    render(<TripWorkspace tripId={captured.id} initialStatus="queued" onStartOver={vi.fn()} />)
    const summary = await screen.findByLabelText('Budget summary')
    expect(summary).toHaveTextContent('40,058.00')
    expect(summary).toHaveTextContent('11,568.00')
    expect(summary).toHaveTextContent('5,430.00')
    expect(summary).toHaveTextContent('14,340.00')
    expect(summary).toHaveTextContent('8,720.00')
    expect(summary).toHaveTextContent('Known costs already exceed your budget by')
    expect(summary).toHaveTextContent('20,058.00')
    expect(summary).toHaveTextContent('Estimate confidence: Low')
    expect(summary).toHaveTextContent('No priced amount')
    expect(summary).toHaveTextContent('historical traveler-reported spending')
    expect(screen.getByText('Your itinerary is available for review.')).toBeInTheDocument()
    expect(screen.getByText(captured.days[0].activities[0].title)).toBeInTheDocument()
  })

  it('ignores delayed status responses from the previously selected trip', async () => {
    let release!: (value: TripStatusResponse) => void
    const oldStatus = new Promise<TripStatusResponse>((resolve) => { release = resolve })
    vi.mocked(getTripStatus).mockReturnValueOnce(oldStatus).mockResolvedValue({ ...completedStatus, trip_id: 'trip-456', task_id: 'trip-456' })
    vi.mocked(getTrip).mockResolvedValue({ ...trip, id: 'trip-456', title: 'Second journey' })
    const view = render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    view.rerender(<TripWorkspace tripId="trip-456" initialStatus="queued" onStartOver={vi.fn()} />)
    expect(await screen.findByText('Second journey')).toBeInTheDocument()
    await act(async () => { release(completedStatus) })
    expect(screen.queryByText('Kyoto, slowly')).not.toBeInTheDocument()
    expect(getTrip).toHaveBeenCalledWith('trip-456', expect.any(AbortSignal))
    expect(getTrip).not.toHaveBeenCalledWith('trip-123', expect.any(AbortSignal))
  })

  it('reports a mismatched final trip response instead of rendering another trip', async () => {
    vi.mocked(getTripStatus).mockResolvedValue(completedStatus)
    vi.mocked(getTrip).mockResolvedValue({ ...trip, id: 'wrong-trip' })
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    expect(await screen.findByText('The server returned details for a different trip.')).toBeInTheDocument()
    expect(screen.queryByText('Kyoto, slowly')).not.toBeInTheDocument()
  })

  it('does not label an explicitly incomplete budget complete when a stale total is present', async () => {
    vi.mocked(getTripStatus).mockResolvedValue({ ...completedStatus, status: 'insufficient_data' })
    vi.mocked(getTrip).mockResolvedValue({ ...trip, status: 'insufficient_data', budget: {
      pricing_status: 'INCOMPLETE', status: 'INCOMPLETE_ESTIMATE', estimated_total: null,
      known_cost_subtotal: '100', unpriced_components: [],
    } })
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    const budget = await screen.findByLabelText('Budget summary')
    expect(budget).toHaveTextContent('Incomplete budget. Required costs are unknown')
    expect(budget).toHaveTextContent('ESTIMATED TOTALNot available')
    expect(budget).not.toHaveTextContent('Complete estimated budget.')
  })
  it('renders persisted accommodation and transport without invented availability or schedules', async () => {
    vi.mocked(getTripStatus).mockResolvedValue(completedStatus)
    vi.mocked(getTrip).mockResolvedValue({ ...trip, accommodation_transport: {
      accommodation: { recommended_types: ['mid_range_hotel'], stay_area_suggestions: ['Provider-listed central area'], room_count: 1, nights: 1 },
      transport_options: [{ mode: 'walking', recommendation_reason: 'Short local visits', route_estimate: {
        distance_kilometers: '1.25', duration_minutes: '18.5', provider: 'openrouteservice', verified_provider_fact: true,
      } }, { mode: 'flight', recommendation_reason: 'Origin to destination', route_estimate: null }],
    } })
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    const section = await screen.findByLabelText('Accommodation and transportation')
    expect(section).toHaveTextContent('Provider-listed central area')
    expect(section).toHaveTextContent('Rooms: 1 · Nights: 1')
    expect(section).toHaveTextContent('1.25 km · 18.5 minutes')
    expect(section).toHaveTextContent('No verified route duration or departure schedule')
    expect(section).toHaveTextContent('Hotel availability and booking prices are unverified')
  })

  it('does not call valid priced items unpriced when a saved summary is inconsistent', async () => {
    vi.mocked(getTripStatus).mockResolvedValue({ ...completedStatus, status: 'insufficient_data' })
    vi.mocked(getTrip).mockResolvedValue({ ...trip, status: 'insufficient_data', total_estimated_cost: null, budget: {
      estimated_total: null, pricing_status: 'INCOMPLETE', known_cost_subtotal: '0.00',
      unpriced_components: ['Accommodation'],
      line_items: [{ category: 'food', description: 'Food', amount: '35.00', currency: 'USD', source: 'user_assumption' }],
    } })
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    const budget = await screen.findByLabelText('Budget summary')
    expect(budget).not.toHaveTextContent('No priced expenses')
    expect(budget).toHaveTextContent('See priced expenses below')
    expect(budget).toHaveTextContent('$35.00')
    expect(budget).toHaveTextContent('subtotal is missing or inconsistent')
  })
  it('retries a transient terminal result-fetch failure and renders the saved itinerary', async () => {
    vi.useFakeTimers()
    vi.mocked(getTripStatus).mockResolvedValue(completedStatus)
    vi.mocked(getTrip).mockRejectedValueOnce(new ApiError('Temporarily unavailable', 503)).mockResolvedValueOnce(trip)
    const view = render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    try {
      await act(async () => { await Promise.resolve() })
      expect(screen.getByText('Temporarily unavailable')).toBeInTheDocument()
      expect(screen.queryByText('Your trip details are available.')).not.toBeInTheDocument()
      await act(async () => { await vi.advanceTimersByTimeAsync(7000) })
      expect(getTrip).toHaveBeenCalledTimes(2)
      expect(screen.getByText('Kyoto, slowly')).toBeInTheDocument()
    } finally { view.unmount(); vi.useRealTimers() }
  })

  it('loads persisted planned records rather than polling indefinitely', async () => {
    vi.useFakeTimers()
    vi.mocked(getTripStatus).mockResolvedValue({ ...completedStatus, status: 'planned' })
    vi.mocked(getTrip).mockResolvedValue({ ...trip, status: 'planned', optimization_review: null })
    const view = render(<TripWorkspace tripId="trip-123" initialStatus="planned" onStartOver={vi.fn()} />)
    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
      expect(getTripStatus).toHaveBeenCalledTimes(1)
      expect(getTrip).toHaveBeenCalledTimes(1)
      expect(screen.getByText('Review the notes below; this plan is not approved.')).toBeInTheDocument()
    } finally { view.unmount(); vi.useRealTimers() }
  })

  it('shows unknown contingency and available itinerary for an incomplete budget', async () => {
    vi.mocked(getTripStatus).mockResolvedValue({ ...completedStatus, status: 'insufficient_data' })
    vi.mocked(getTrip).mockResolvedValue({ ...trip, status: 'insufficient_data', total_estimated_cost: null, budget: {
      estimated_total: null, pricing_status: 'INCOMPLETE', known_cost_subtotal: '0',
      unpriced_components: ['Accommodation', 'Contingency reserve'],
      category_totals: [{ category: 'contingency', known_subtotal: '0', complete: false, included: true },
        { category: 'other', known_subtotal: '0', complete: false, included: false }],
      line_items: [{ category: 'contingency', description: 'Contingency reserve', amount: null, currency: 'USD', source: 'unknown' }],
    } })
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    const budget = await screen.findByLabelText('Budget summary')
    expect(budget).toHaveTextContent('No priced amount')
    expect(budget).toHaveTextContent('No priced expenses')
    expect(budget).toHaveTextContent('Not included')
    expect(screen.getByText('Kyoto, slowly')).toBeInTheDocument()
  })
  it('renders automatic reference estimates, scenario bounds, provenance and booking warnings', async () => {
    vi.mocked(getTripStatus).mockResolvedValue(completedStatus)
    vi.mocked(getTrip).mockResolvedValue({
      ...trip, budget: {
        pricing_status: 'COMPLETE_ESTIMATE', estimated_total: '1200.00',
        known_cost_subtotal: '1200.00',
        minimum_estimated_total: '1000.00', maximum_estimated_total: '1600.00',
        estimate_confidence: 'low', remaining_budget: '2300.00', unpriced_components: [],
        line_items: [{ description: 'Accommodation', amount: '300.00', currency: 'USD',
          classification: 'estimated', source: 'reference_data', evidence_kind: 'planning_estimate',
          minimum_amount: '200.00', maximum_amount: '400.00', confidence: 'low',
          price_sources: [{ name: 'Test reference source', url: 'https://example.org/reference', observed_on: '2026-10-11', currency: 'JPY' }],
          assumptions: ['Double occupancy, three nights; planning assumption.'],
          currency_conversion: { original_currency: 'JPY', target_currency: 'USD', rate: '0.0065', rate_date: '2026-10-09', source: 'Reference FX source' },
        }],
      },
    })
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    const budget = await screen.findByLabelText('Budget summary')
    expect(budget).toHaveTextContent('Reference-based planning estimate · not a live quote')
    expect(budget).toHaveTextContent('Estimate confidence: Low')
    expect(budget).toHaveTextContent('Double occupancy, three nights')
    expect(budget).toHaveTextContent('Observed 2026-10-11')
    expect(budget).toHaveTextContent('dated 2026-10-09')
    expect(screen.getByLabelText('Planning cost range')).toHaveTextContent('1,000.00')
    expect(screen.getByLabelText('Planning cost range')).toHaveTextContent('1,600.00')
    expect(within(budget).getByRole('link', { name: 'Test reference source' })).toHaveAttribute('href', 'https://example.org/reference')
    expect(budget).toHaveTextContent('Booking prices and availability remain unverified')
    expect(budget).not.toHaveTextContent('Live provider price')
  })

  it('shows an actionable partial budget without a zero total or an unsupported range', async () => {
    vi.mocked(getTripStatus).mockResolvedValue({ ...completedStatus, status: 'insufficient_data' })
    vi.mocked(getTrip).mockResolvedValue({ ...trip, total_estimated_cost: null, budget: {
      pricing_status: 'INCOMPLETE', estimated_total: null, minimum_estimated_total: null, maximum_estimated_total: null,
      known_cost_subtotal: '200.00', remaining_budget: null, unpriced_components: ['food'],
      missing_price_actions: ['food: obtain a dated JPY/USD reference exchange rate or supply an allowance in USD.'],
      line_items: [{ description: 'Food', amount: null, currency: 'USD', classification: 'unknown', source: 'unknown' }],
    } })
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    const budget = await screen.findByLabelText('Budget summary')
    expect(budget).toHaveTextContent('Not available')
    expect(budget).toHaveTextContent('200.00')
    expect(screen.getByLabelText('Resolve missing prices')).toHaveTextContent('dated JPY/USD reference exchange rate')
    expect(screen.queryByLabelText('Planning cost range')).not.toBeInTheDocument()
    expect(budget).not.toHaveTextContent('Remaining budget')
  })

  it('labels a negative remaining budget as an overrun', async () => {
    vi.mocked(getTripStatus).mockResolvedValue({ ...completedStatus, status: 'revision_required' })
    vi.mocked(getTrip).mockResolvedValue({ ...trip, budget: {
      pricing_status: 'COMPLETE_ESTIMATE', status: 'OVER_BUDGET', estimated_total: '4000.00', remaining_budget: '-500.00',
    } })
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    const budget = await screen.findByLabelText('Budget summary')
    expect(budget).toHaveTextContent('Budget overrun')
    expect(budget).toHaveTextContent('500.00')
    expect(budget).not.toHaveTextContent('Remaining budget')
  })

  it('renders a complete INR planning estimate without claiming live prices', async () => {
    vi.mocked(getTripStatus).mockResolvedValue(completedStatus)
    vi.mocked(getTrip).mockResolvedValue({
      ...trip, currency: 'INR', total_estimated_cost: '185900.00',
      request: { ...trip.request, total_budget: '200000.00', currency: 'INR' },
      budget: {
        pricing_status: 'COMPLETE_ESTIMATE', estimated_total: '185900.00',
        known_cost_subtotal: '185900.00', remaining_budget: '14100.00',
        unpriced_components: [], category_totals: [{
          category: 'accommodation', known_subtotal: '60000.00', complete: true,
        }],
        line_items: [{
          description: 'Accommodation', amount: '60000.00', currency: 'INR',
          classification: 'estimated', source: 'user_assumption',
          pricing_basis: '5000 per room × 1 room × 12 nights.',
        }],
      },
    })
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    const budget = await screen.findByLabelText('Budget summary')
    expect(budget).toHaveTextContent(/1,?85,900\.00/)
    expect(budget).toHaveTextContent(/2,?00,000\.00/)
    expect(budget).toHaveTextContent('14,100.00')
    expect(budget).toHaveTextContent('60,000.00')
    expect(budget).toHaveTextContent('Complete estimated budget')
    expect(budget).toHaveTextContent('User-provided planning estimate')
    expect(screen.queryByText('Live provider price')).not.toBeInTheDocument()
  })

  it('names exactly the missing cost field instead of presenting a zero total', async () => {
    vi.mocked(getTripStatus).mockResolvedValue({ ...completedStatus, status: 'insufficient_data' })
    vi.mocked(getTrip).mockResolvedValue({
      ...trip, status: 'insufficient_data', currency: 'INR', total_estimated_cost: null,
      optimization_review: { approved: false, review_status: 'INSUFFICIENT_DATA' },
      budget: {
        pricing_status: 'INCOMPLETE', estimated_total: null, remaining_budget: null,
        missing_price_inputs: ['food_allowance_per_traveler_per_day'],
        unpriced_components: ['food: Food'],
        line_items: [{ description: 'Food', amount: null, currency: 'INR', classification: 'unknown', source: 'unknown' }],
      },
    })
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    expect(await screen.findByLabelText('Missing information')).toHaveTextContent('Food per traveler per day (INR)')
    expect(screen.getByLabelText('Budget summary')).toHaveTextContent('Incomplete budget')
    expect(screen.getByLabelText('Budget summary')).toHaveTextContent('Not available')
    expect(screen.getByLabelText('Budget summary')).toHaveTextContent('Unknown')
  })
  it.each([
    'Destination research could not provide the minimum verified data needed to plan.',
    'Destination research: clarification_required. Choose a destination city within the requested area.',
  ])('displays the actual destination research failure: %s', async (errorMessage) => {
    vi.mocked(getTripStatus).mockResolvedValue({
      ...completedStatus, status: 'insufficient_data', current_stage: 'destination_research',
      error_message: errorMessage,
    })
    vi.mocked(getTrip).mockResolvedValue({
      ...trip, status: 'insufficient_data', itinerary: null, days: [], budget: null,
      optimization_review: null,
      warnings: ['Use a specific destination city and country.'],
    })
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    expect(await screen.findByText(errorMessage)).toBeInTheDocument()
    expect(await screen.findByText('Use a specific destination city and country.')).toBeInTheDocument()
    expect(screen.queryByText('Your itinerary is ready.')).not.toBeInTheDocument()
  })

  it('shows detail retrieval errors for incomplete plans', async () => {
    vi.mocked(getTripStatus).mockResolvedValue({ ...completedStatus, status: 'insufficient_data' })
    vi.mocked(getTrip).mockRejectedValue(new Error('Trip details are temporarily unavailable.'))
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Trip details are temporarily unavailable.')
  })
  it('polls status and displays a completed itinerary and budget', async () => {
    vi.mocked(getTripStatus).mockResolvedValue(completedStatus)
    vi.mocked(getTrip).mockResolvedValue(trip)

    render(
      <TripWorkspace
        tripId="trip-123"
        initialStatus="queued"
        onStartOver={vi.fn()}
      />,
    )

    expect(await screen.findByText('Kyoto, slowly')).toBeInTheDocument()
    expect(screen.getByText('Visit the garden')).toBeInTheDocument()
    expect(screen.getByText('A gentle first day')).toBeInTheDocument()
    expect(screen.getByLabelText('Budget summary')).toHaveTextContent('$1,200.00')
    expect(getTripStatus).toHaveBeenCalledWith('trip-123', expect.any(AbortSignal))
  })

  it('renders the canonical optimization recommendation returned by the API', async () => {
    vi.mocked(getTripStatus).mockResolvedValue({
      ...completedStatus,
      status: 'revision_required',
    })
    vi.mocked(getTrip).mockResolvedValue({
      ...trip,
      status: 'revision_required',
      optimization_review: {
        approved: false,
        review_status: 'REVISION_REQUIRED',
        suggestions: [{
          category: 'routing',
          recommendation: 'Group nearby activities to reduce unnecessary travel.',
          estimated_savings: null,
          currency: 'USD',
          source_references: [],
        }],
      },
    })

    render(
      <TripWorkspace
        tripId="trip-123"
        initialStatus="queued"
        onStartOver={vi.fn()}
      />,
    )

    expect(
      await screen.findByText(
        'Group nearby activities to reduce unnecessary travel.',
      ),
    ).toBeInTheDocument()
    expect(
      screen.queryByText('An improvement was suggested.'),
    ).not.toBeInTheDocument()
  })

  it('shows failed workflow states without exposing internals', async () => {
    vi.mocked(getTripStatus).mockResolvedValue({
      ...completedStatus,
      status: 'failed',
      error_message: 'Planning could not be completed.',
    })
    vi.mocked(getTrip).mockResolvedValue({ ...trip, status: 'failed', optimization_review: null })
    render(
      <TripWorkspace
        tripId="trip-123"
        initialStatus="queued"
        onStartOver={vi.fn()}
      />,
    )
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Planning could not be completed.',
    )
    expect(await screen.findByText('Kyoto, slowly')).toBeInTheDocument()
    expect(getTrip).toHaveBeenCalledTimes(1)
  })

  it('communicates missing itinerary data instead of inventing activities', async () => {
    vi.mocked(getTripStatus).mockResolvedValue(completedStatus)
    vi.mocked(getTrip).mockResolvedValue({ ...trip, days: [] })
    render(
      <TripWorkspace
        tripId="trip-123"
        initialStatus="queued"
        onStartOver={vi.fn()}
      />,
    )
    await waitFor(() =>
      expect(
        screen.getByText('Day-by-day details aren’t available yet.'),
      ).toBeInTheDocument(),
    )
  })

  it('shows available partial itinerary when required information is missing', async () => {
    vi.mocked(getTripStatus).mockResolvedValue({
      ...completedStatus,
      status: 'insufficient_data',
    })
    vi.mocked(getTrip).mockResolvedValue({
      ...trip,
      status: 'insufficient_data',
      optimization_review: { approved: false, review_status: 'INSUFFICIENT_DATA' },
      warnings: ['Accommodation price is unknown.'],
    })
    render(
      <TripWorkspace
        tripId="trip-123"
        initialStatus="queued"
        onStartOver={vi.fn()}
      />,
    )

    expect(await screen.findByText('Visit the garden')).toBeInTheDocument()
    expect(screen.getByText('Accommodation price is unknown.')).toBeInTheDocument()
    expect(screen.getByText(/not approved/i)).toBeInTheDocument()
  })

  it('preserves unknown categories and priced items when the saved subtotal is inconsistent', async () => {
    vi.mocked(getTripStatus).mockResolvedValue(completedStatus)
    vi.mocked(getTrip).mockResolvedValue({
      ...trip,
      budget: {
        known_cost_subtotal: '0.00',
        estimated_total: null,
        remaining_budget: null,
        status: 'INCOMPLETE_ESTIMATE',
        unpriced_components: ['food'],
        category_totals: [
          {
            category: 'food',
            known_subtotal: '0.00',
            complete: false,
          },
        ],
        line_items: [
          {
            category: 'food',
            description: 'Food allowance',
            amount: null,
            currency: 'USD',
            classification: 'unknown',
            source: 'unknown',
          },
          {
            category: 'accommodation',
            description: 'Accommodation',
            amount: '100.00',
            currency: 'USD',
            classification: 'estimated',
            source: 'user_assumption',
            pricing_basis: 'User-provided estimate, not a live quote.',
          },
        ],
      },
      total_estimated_cost: null,
    })
    render(
      <TripWorkspace
        tripId="trip-123"
        initialStatus="queued"
        onStartOver={vi.fn()}
      />,
    )
    expect(await screen.findByText('See priced expenses below')).toBeInTheDocument()
    expect(screen.queryByText('No priced expenses')).not.toBeInTheDocument()
    expect(screen.getByText('No priced amount')).toBeInTheDocument()
    expect(screen.getByText('User-provided planning estimate')).toBeInTheDocument()
    expect(screen.getAllByText('Not available').length).toBeGreaterThan(0)
  })

  it('shows active progress and an accessible progress bar', async () => {
    vi.mocked(getTripStatus).mockResolvedValue({
      ...completedStatus,
      status: 'running',
      progress_percentage: 45,
      current_stage: 'budget',
    })
    render(
      <TripWorkspace
        tripId="trip-123"
        initialStatus="queued"
        onStartOver={vi.fn()}
      />,
    )
    expect(await screen.findByRole('progressbar')).toHaveAttribute(
      'aria-valuenow',
      '45',
    )
  })

  it('polls again while the workflow is running and stops after completion', async () => {
    vi.useFakeTimers()
    vi.mocked(getTripStatus)
      .mockResolvedValueOnce({
        ...completedStatus,
        status: 'running',
        current_stage: 'itinerary',
        progress_percentage: 72,
      })
      .mockResolvedValueOnce(completedStatus)
    vi.mocked(getTrip).mockResolvedValue(trip)

    const view = render(
      <TripWorkspace
        tripId="trip-123"
        initialStatus="queued"
        onStartOver={vi.fn()}
      />,
    )

    try {
      await act(async () => {
        await Promise.resolve()
      })
      expect(getTripStatus).toHaveBeenCalledTimes(1)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3500)
      })
      expect(getTripStatus).toHaveBeenCalledTimes(2)
      expect(getTrip).toHaveBeenCalledTimes(1)
      expect(screen.getByText('Kyoto, slowly')).toBeInTheDocument()
    } finally {
      view.unmount()
      vi.useRealTimers()
    }
  })

  it('renders a repeated provider warning only once', async () => {
    vi.mocked(getTripStatus).mockResolvedValue(completedStatus)
    vi.mocked(getTrip).mockResolvedValue({ ...trip, warnings: ['Gemini: provider request timed out.', 'Gemini: provider request timed out.'] })
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    await screen.findByText('Kyoto, slowly')
    expect(screen.getAllByText(/Gemini: provider request timed out/)).toHaveLength(1)
  })

  it('stops polling after a permanent missing-trip error', async () => {
    vi.useFakeTimers()
    vi.mocked(getTripStatus).mockRejectedValue(new ApiError('This trip could not be found.', 404))
    const view = render(<TripWorkspace tripId="missing" initialStatus="queued" onStartOver={vi.fn()} />)
    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
      expect(getTripStatus).toHaveBeenCalledTimes(1)
      expect(screen.getByText('This trip could not be found.')).toBeInTheDocument()
    } finally { view.unmount(); vi.useRealTimers() }
  })

  it('aborts active polling and clears the next poll on unmount', async () => {
    vi.useFakeTimers()
    vi.mocked(getTripStatus).mockResolvedValue({ ...completedStatus, status: 'running' })
    const view = render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    try {
      await act(async () => { await Promise.resolve() })
      const signal = vi.mocked(getTripStatus).mock.calls[0][1]
      view.unmount()
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
      expect(signal?.aborted).toBe(true)
      expect(getTripStatus).toHaveBeenCalledTimes(1)
      expect(getTrip).not.toHaveBeenCalled()
    } finally { view.unmount(); vi.useRealTimers() }
  })
})
