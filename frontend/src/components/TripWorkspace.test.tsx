import { act, render, screen, waitFor, within } from '@testing-library/react'
import { StrictMode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { ApiError, getTrip, getTripStatus, reviseTrip } from '../api'
import type { CachedFlightReference, PersistedActivity, TripDetails, TripStatusResponse } from '../types'
import { TripWorkspace } from './TripWorkspace'
import capturedPartialResponse from '../test-fixtures/real-partial-budget-20261012.json'

vi.mock('../api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../api')>(),
  getTrip: vi.fn(),
  getTripStatus: vi.fn(),
  reviseTrip: vi.fn(),
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

function flightReference(): CachedFlightReference {
  const retrieved = new Date(Date.now() - 60_000)
  return {
    provider: 'travelpayouts', classification: 'cached_reference', live_availability_checked: false,
    origin_iata: 'SEA', destination_iata: 'KIX', departure_date: trip.request.start_date, return_date: trip.request.end_date,
    currency: 'USD', price: '600.00', retrieved_at: retrieved.toISOString(),
    found_at: new Date(retrieved.getTime() - 3_600_000).toISOString(),
    expires_at: new Date(Date.now() + 3_600_000).toISOString(), applicability: 'applicable',
    passenger_basis: { kind: 'per_adult', priced_passengers: 1, evidence: 'Explicit fictional passenger-basis fixture.' },
    requested_scope: { origin_iata: 'SEA', destination_iata: 'KIX', origin_location: trip.request.origin,
      destination_location: trip.request.destination, departure_date: trip.request.start_date, return_date: trip.request.end_date,
      currency: 'USD', travelers: 2 },
  }
}

function cachedFlightTrip(reference = flightReference()): TripDetails {
  return { ...trip, budget: { estimated_total: '1200.00', known_cost_subtotal: '1200.00',
    status: 'WITHIN_BUDGET', pricing_status: 'COMPLETE_ESTIMATE', booking_readiness: 'unverified', estimate_confidence: 'low',
    category_totals: [{ category: 'intercity_transport', known_subtotal: '1200.00', currency: 'USD', complete: true, included: true }],
    flight_references: [reference], line_items: [{ category: 'intercity_transport', description: 'Round-trip flight reference',
      amount: '1200.00', currency: 'USD', source: 'reference_data', classification: 'estimated', evidence_kind: 'planning_estimate',
      confidence: 'low', flight_reference: reference,
      source_references: [{ provider: 'travelpayouts', title: 'Cached flight source', url: 'https://api.travelpayouts.com/aviasales/v3/prices_for_dates' }],
    }],
  } }
}

describe('TripWorkspace', () => {
  it.each([
    { label: 'null attraction price', value: null, category: 'attraction' },
    { label: 'undefined meal price', value: undefined, category: 'meal_break' },
    { label: 'omitted transport price', value: undefined, category: 'transport', omit: true },
    { label: 'unknown arrival placeholder', value: '0.00', category: 'arrival_departure', metadata: { estimated_cost_unknown: true } },
    { label: 'unknown positive price', value: '12.00', category: 'attraction', metadata: { estimated_cost_unknown: true } },
    { label: 'unknown classification', value: '12.00', category: 'meal_break', metadata: { cost_classification: 'unknown' } },
    { label: 'blank price', value: ' ', category: 'activity' },
    { label: 'non-numeric price', value: 'N/A', category: 'activity' },
    { label: 'non-finite price', value: 'Infinity', category: 'activity' },
    { label: 'negative price', value: '-1.00', category: 'activity' },
  ])('leaves the activity price position empty for $label', async ({ value, category, omit, metadata }) => {
    const activity = { ...trip.days[0].activities[0], category, estimated_cost: value, metadata: metadata ?? {} }
    if (omit) Reflect.deleteProperty(activity, 'estimated_cost')
    vi.mocked(getTripStatus).mockResolvedValue(completedStatus)
    // Exercise missing runtime fields although the persisted type requires a nullable price.
    vi.mocked(getTrip).mockResolvedValue({ ...trip, days: [{ ...trip.days[0], activities: [activity as PersistedActivity] }] })
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    const heading = await screen.findByRole('heading', { name: activity.title })
    const card = heading.closest('li')!
    expect(card.querySelector('.activity-title-row > span')).toBeNull()
    expect(card).not.toHaveTextContent(/Price not available|Not supplied|Not available|N\/A|₹0|USD\s*0\.00/)
    expect(card).toHaveTextContent(activity.description!)
    expect(card).toHaveTextContent(activity.location_name!)
    expect(card).toHaveTextContent('09:00:00 – 10:30:00')
    expect(screen.getByRole('article', { name: 'Day 1 itinerary' })).toHaveTextContent('A gentle first day')
  })

  it.each([
    ['12.00', 'USD', 'USD 12.00'],
    ['25.50', 'INR', 'INR 25.50'],
    ['0.00', 'INR', 'INR 0.00'],
  ])('keeps a supplied known activity price %s in %s visible', async (amount, currency, expected) => {
    vi.mocked(getTripStatus).mockResolvedValue(completedStatus)
    vi.mocked(getTrip).mockResolvedValue({ ...trip, days: [{ ...trip.days[0], activities: [{
      ...trip.days[0].activities[0], estimated_cost: amount,
      metadata: { cost_currency: currency, cost_classification: 'estimated', estimated_cost_unknown: false },
    }] }] })
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    const heading = await screen.findByRole('heading', { name: 'Visit the garden' })
    expect(heading.closest('li')!.querySelector('.activity-title-row > span')).toHaveTextContent(expected)
    expect(screen.getByLabelText('Budget summary')).toHaveTextContent('USD 1,200.00')
  })

  it('shows a provider-listed food venue as a meal break with unverified hours and price', async () => {
    vi.mocked(getTripStatus).mockResolvedValue(completedStatus)
    vi.mocked(getTrip).mockResolvedValue({
      ...trip,
      days: [{ ...trip.days[0], activities: [{
        ...trip.days[0].activities[0], title: 'Hotel Udupi Krishna', category: 'meal_break',
        location_name: 'Hotel Udupi Krishna, MDR48, Chaudi, Canacona - 403702, Goa, India',
        description: 'Food stop at the provider-listed venue Hotel Udupi Krishna. Meal availability and prices are not verified. Confirm current opening hours before departure.',
        estimated_cost: null,
        metadata: { coordinates_verified: true, latitude: 15.0072353, longitude: 74.0466556,
          estimated_cost_unknown: true, warnings: ['Estimated visit duration; opening hours were not verified.'] },
      }] }],
    })
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    const venue = await screen.findByRole('heading', { name: 'Hotel Udupi Krishna' })
    const card = venue.closest('li')!
    expect(within(card).getByText('Meal Break')).toBeInTheDocument()
    expect(within(card).queryByText('Price not available')).not.toBeInTheDocument()
    expect(card.querySelector('.activity-title-row > span')).toBeNull()
    expect(within(card).getByText(/opening hours were not verified/)).toBeInTheDocument()
    expect(within(card).queryByText('Attraction')).not.toBeInTheDocument()
  })

  it('displays selected cached flight metadata without implying live booking prices', async () => {
    const reference = flightReference()
    vi.mocked(getTripStatus).mockResolvedValue(completedStatus)
    vi.mocked(getTrip).mockResolvedValue(cachedFlightTrip(reference))
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    const evidence = await screen.findByLabelText('Flight Cost Estimate')
    expect(evidence).toHaveTextContent('Provider: Travelpayouts')
    expect(evidence).toHaveTextContent('Route: SEA → KIX')
    expect(evidence).toHaveTextContent(`Departure: ${trip.request.start_date} · Return: ${trip.request.end_date}`)
    expect(evidence).toHaveTextContent(`Retrieved: ${reference.retrieved_at}`)
    expect(evidence).toHaveTextContent(`Observed: ${reference.found_at}`)
    expect(evidence).toHaveTextContent(`Expires: ${reference.expires_at}`)
    expect(evidence).toHaveTextContent('Passenger pricing basis: Per Adult · 1 priced passenger(s)')
    expect(evidence).toHaveTextContent('Currency: USD')
    expect(evidence).toHaveTextContent('Evidence: Planning Estimate')
    expect(evidence).toHaveTextContent('Confidence: Low')
    expect(evidence).toHaveTextContent('Not live availability or a confirmed booking price')
    expect(screen.queryByText('Live provider price')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Budget summary')).toHaveTextContent('1,200.00')
    expect(screen.getByText('Visit the garden')).toBeInTheDocument()
  })

  it('labels an included user flight allowance without inventing provider metadata', async () => {
    vi.mocked(getTripStatus).mockResolvedValue(completedStatus)
    vi.mocked(getTrip).mockResolvedValue({ ...trip, budget: { pricing_status: 'COMPLETE_ESTIMATE',
      estimated_total: '1200.00', known_cost_subtotal: '1200.00', line_items: [{ category: 'intercity_transport',
        description: 'Flight allowance', amount: '1200.00', currency: 'USD', source: 'user_assumption', classification: 'estimated',
        evidence_kind: 'planning_estimate', pricing_basis: 'Traveler supplied a whole-party allowance.' }],
    } })
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    const evidence = await screen.findByLabelText('Flight Cost Estimate')
    expect(evidence).toHaveTextContent('User-provided planning estimate')
    expect(evidence).toHaveTextContent('A user-provided flight allowance is included; no provider fare reference was returned')
    expect(evidence).not.toHaveTextContent('Provider:')
    expect(evidence).not.toHaveTextContent('Route:')
    expect(evidence).not.toHaveTextContent('Retrieved:')
    expect(evidence).not.toHaveTextContent('Live provider price')
  })

  it.each(['date_mismatch', 'passenger_basis_unknown_or_mismatch', 'stale_reference'])('keeps rejected %s fares outside included costs', async (reason) => {
    const reference = { ...flightReference(), applicability: reason, price: '9999.00' }
    const partial = { ...trip, status: 'insufficient_data' as const, total_estimated_cost: null,
      optimization_review: { approved: false, review_status: 'INSUFFICIENT_DATA' as const },
      budget: { estimated_total: null, known_cost_subtotal: '200.00', pricing_status: 'INCOMPLETE' as const,
        flight_references: [reference], unpriced_components: ['intercity_transport'],
        line_items: [{ category: 'intercity_transport' as const, description: 'Flight price', amount: null, currency: 'USD', source: 'unknown' as const }],
      },
    }
    vi.mocked(getTripStatus).mockResolvedValue({ ...completedStatus, status: 'insufficient_data' })
    vi.mocked(getTrip).mockResolvedValue(partial)
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    expect(await screen.findByLabelText('Excluded flight references')).toHaveTextContent('References not included in budget costs')
    expect(screen.getByLabelText('Flight Cost Estimate')).toHaveTextContent('No applicable flight price is included')
    expect(screen.getByLabelText('Budget summary')).toHaveTextContent('Not available')
    expect(screen.queryByText(/9,999\.00/)).not.toBeInTheDocument()
    expect(screen.getByText('Visit the garden')).toBeInTheDocument()
  })

  it.each(['wrong_dates', 'expired', 'wrong_route', 'unknown_basis'])('rejects an attached reference with invalid %s even if its saved flag says applicable', async (invalid) => {
    const reference = flightReference()
    if (invalid === 'wrong_dates') reference.departure_date = '2026-10-10'
    if (invalid === 'expired') reference.expires_at = '2020-01-01T00:00:00Z'
    if (invalid === 'wrong_route') reference.origin_iata = 'HYD'
    if (invalid === 'unknown_basis') reference.passenger_basis = { kind: 'unknown' }
    vi.mocked(getTripStatus).mockResolvedValue(completedStatus)
    vi.mocked(getTrip).mockResolvedValue(cachedFlightTrip(reference))
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    const budget = await screen.findByLabelText('Budget summary')
    expect(budget).toHaveTextContent('Not available')
    expect(budget).toHaveTextContent('Flight evidence does not apply to this trip')
    expect(budget).not.toHaveTextContent('1,200.00')
    expect(screen.getByLabelText('Excluded flight references')).not.toHaveTextContent('1,200.00')
    expect(screen.queryByText('Reviewed')).not.toBeInTheDocument()
  })

  it('keeps missing AED flight prices unavailable while displaying all five itinerary days', async () => {
    const days = Array.from({ length: 5 }, (_, index) => ({ ...trip.days[0], day_number: index + 1,
      itinerary_date: `2026-11-${10 + index}`, estimated_daily_cost: null, activities: [] }))
    vi.mocked(getTripStatus).mockResolvedValue({ ...completedStatus, status: 'insufficient_data' })
    vi.mocked(getTrip).mockResolvedValue({ ...trip, status: 'insufficient_data', total_estimated_cost: null, currency: 'AED', days,
      request: { ...trip.request, currency: 'AED', start_date: '2026-11-10', end_date: '2026-11-14' },
      optimization_review: { approved: false, review_status: 'INSUFFICIENT_DATA' },
      budget: { pricing_status: 'INCOMPLETE', estimated_total: null, known_cost_subtotal: '4961.00',
        unpriced_components: ['intercity_transport'], line_items: [{ category: 'intercity_transport',
          description: 'Flight price', amount: null, currency: 'AED', classification: 'unknown', source: 'unknown' }],
      },
    })
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    const budget = await screen.findByLabelText('Budget summary')
    expect(budget).toHaveTextContent('Not available')
    expect(budget.textContent).not.toMatch(/AED\s*0(?:\.00)?/)
    expect(screen.getByLabelText('Flight Cost Estimate')).toHaveTextContent('Flight pricing remains unavailable')
    expect(screen.getAllByText(/^DAY \d$/)).toHaveLength(5)
  })

  it.each([['budget_revision', 'Revising budget'], ['revision', 'Rechecking itinerary']])('shows %s progress and the original status message', async (stage, label) => {
    vi.mocked(getTripStatus).mockResolvedValue({ ...completedStatus, status: 'running', current_stage: stage,
      message: 'Supported changes are being checked.', progress_percentage: 45 })
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    expect(await screen.findByLabelText('Current revision')).toHaveTextContent(label)
    expect(screen.getByLabelText('Planning status message')).toHaveTextContent('Supported changes are being checked.')
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '45')
    expect(screen.getByRole('progressbar').firstElementChild).toHaveStyle({ width: '45%' })
    expect(getTrip).not.toHaveBeenCalled()
  })

  it('shows reviewer summary, warnings and constraint actions without implying approval', async () => {
    vi.mocked(getTripStatus).mockResolvedValue({ ...completedStatus, status: 'revision_required' })
    vi.mocked(getTrip).mockResolvedValue({ ...trip, status: 'revision_required', optimization_review: {
      approved: false, review_status: 'REVISION_REQUIRED', qualitative_summary: 'Review the required accessibility constraints.',
      warnings: ['Accessibility evidence is missing.', 'Accessibility evidence is missing.'],
      issues: [{ description: 'A required step-free route is not verified.', blocks_approval: true,
        evidence_basis: 'The provider did not supply access evidence.', recommended_action: 'Confirm a step-free alternative.' }],
      suggestions: [{ category: 'accessibility', recommendation: 'Review accessible transport options.' }],
    } })
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    const review = await screen.findByLabelText('Planner review')
    expect(within(review).getByLabelText('Reviewer summary')).toHaveTextContent('Review the required accessibility constraints.')
    expect(within(review).getByLabelText('Reviewer warnings')).toHaveTextContent('Accessibility evidence is missing.')
    expect(within(review).getAllByText('Accessibility evidence is missing.')).toHaveLength(1)
    expect(review).toHaveTextContent('Blocks approval')
    expect(review).toHaveTextContent('Evidence: The provider did not supply access evidence.')
    expect(review).toHaveTextContent('Recommended action: Confirm a step-free alternative.')
    expect(review).toHaveTextContent('Review accessible transport options.')
    expect(review).toHaveTextContent('Reviewer approval is not confirmed.')
  })

  it('shows revision status and details retrieval errors without hiding the result state', async () => {
    vi.mocked(getTripStatus).mockResolvedValue({ ...completedStatus, status: 'revision_required',
      error_message: 'Supported costs still exceed the budget.' })
    vi.mocked(getTrip).mockRejectedValue(new ApiError('Trip details are temporarily unavailable.', 503))
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Trip details are temporarily unavailable.')
    expect(screen.getByText('Supported costs still exceed the budget.')).toBeInTheDocument()
    expect(screen.getByText('Your plan needs a few adjustments.')).toBeInTheDocument()
  })

  it('retains the existing retry behavior after revision details fail', async () => {
    vi.useFakeTimers()
    vi.mocked(getTripStatus).mockResolvedValue({ ...completedStatus, status: 'revision_required' })
    vi.mocked(getTrip).mockRejectedValueOnce(new ApiError('Details retry needed.', 503))
      .mockResolvedValue({ ...trip, status: 'revision_required' })
    const view = render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      expect(screen.getByRole('alert')).toHaveTextContent('Details retry needed.')
      await act(async () => { await vi.advanceTimersByTimeAsync(7000) })
      expect(getTrip).toHaveBeenCalledTimes(2)
      expect(screen.getByText('Visit the garden')).toBeInTheDocument()
      expect(screen.queryByText('Details retry needed.')).not.toBeInTheDocument()
      await act(async () => { await vi.advanceTimersByTimeAsync(14_000) })
      expect(getTrip).toHaveBeenCalledTimes(2)
    } finally { view.unmount(); vi.useRealTimers() }
  })

  it.each([
    null,
    { approved: false, review_status: 'APPROVED' as const },
    { approved: true, review_status: 'REVISION_REQUIRED' as const },
    { approved: true, review_status: 'APPROVED' as const, issues: [{ description: 'Unresolved issue', blocks_approval: true }] },
  ])('shows completed itinerary without implying approval for missing or inconsistent review %j', async (review) => {
    vi.mocked(getTripStatus).mockResolvedValue(completedStatus)
    vi.mocked(getTrip).mockResolvedValue({ ...trip, optimization_review: review,
      budget: { status: 'WITHIN_BUDGET', pricing_status: 'COMPLETE_ESTIMATE', estimated_total: '7601.00',
        estimate_confidence: 'low', booking_readiness: 'unverified' } })
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    expect(await screen.findByText('Visit the garden')).toBeInTheDocument()
    expect(screen.getByText('Review needed')).toBeInTheDocument()
    expect(screen.queryByText('Reviewed')).not.toBeInTheDocument()
    expect(screen.getByText('Review the notes below; this plan is not approved.')).toBeInTheDocument()
    expect(screen.getByLabelText('Budget summary')).toHaveTextContent('Estimate confidence: Low')
    expect(screen.getByLabelText('Budget summary')).toHaveTextContent('Booking prices and availability remain unverified')
  })

  it('labels an explicitly approved completed itinerary as reviewed without claiming booking readiness', async () => {
    vi.mocked(getTripStatus).mockResolvedValue(completedStatus)
    vi.mocked(getTrip).mockResolvedValue(trip)
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    expect(await screen.findByText('Reviewed')).toBeInTheDocument()
    expect(screen.getByText('Visit the garden')).toBeInTheDocument()
    expect(screen.getByLabelText('Budget summary')).toHaveTextContent('Booking prices and availability remain unverified')
  })

  it.each([
    { evidence_kind: undefined }, { evidence_kind: 'planning_estimate' as const },
    { source: 'user_assumption' as const }, { source: 'reference_data' as const },
    { source_references: [] },
    { source_references: [{ title: 'Cached reference', url: 'https://api.travelpayouts.com/aviasales/v3/prices_for_dates', provider: 'travelpayouts' }] },
  ])('does not promote insufficient or non-live metadata to verified prices %j', async (metadata) => {
    vi.mocked(getTripStatus).mockResolvedValue(completedStatus)
    vi.mocked(getTrip).mockResolvedValue({ ...trip, budget: {
      estimated_total: '100.00', known_cost_subtotal: '100.00', pricing_status: 'LIVE_PRICE_VERIFIED', line_items: [{
        amount: '100.00', currency: 'USD', description: 'Test cost', source: 'verified_provider',
        classification: 'verified', evidence_kind: 'verified_price',
        source_references: [{ title: 'Provider evidence', url: 'https://api.amadeus.com/v2/shopping/flight-offers', provider: 'amadeus' }],
        ...metadata,
      }],
    } })
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    const budget = await screen.findByLabelText('Budget summary')
    expect(budget).not.toHaveTextContent('Live provider price')
    expect(budget).not.toHaveTextContent('Verified provider price')
    expect(budget).not.toHaveTextContent('Verified-price budget')
    if (metadata.source_references?.some((source) => source.provider === 'travelpayouts')) {
      expect(budget).toHaveTextContent('Flight evidence does not apply to this trip')
      expect(budget).toHaveTextContent('Incomplete budget')
    } else {
      expect(budget).toHaveTextContent('Planning estimates; prices and availability are not live-verified.')
    }
  })

  it('labels prices as verified only with consistent evidence and keeps booking unverified', async () => {
    vi.mocked(getTripStatus).mockResolvedValue(completedStatus)
    vi.mocked(getTrip).mockResolvedValue({ ...trip, budget: {
      estimated_total: '100.00', known_cost_subtotal: '100.00', pricing_status: 'LIVE_PRICE_VERIFIED', booking_readiness: 'unverified',
      line_items: [{ description: 'Verified flight quote', amount: '100.00', currency: 'USD',
        source: 'verified_provider', classification: 'verified', evidence_kind: 'verified_price',
        source_references: [{ title: 'Production search', provider: 'amadeus', url: 'https://api.amadeus.com/v2/shopping/flight-offers' }],
      }],
    } })
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    const budget = await screen.findByLabelText('Budget summary')
    expect(budget).toHaveTextContent('Verified provider price')
    expect(budget).toHaveTextContent('Verified-price budget')
    expect(budget).toHaveTextContent('Booking prices and availability still require confirmation.')
  })

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
    expect(budget).toHaveTextContent('USD 35.00')
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
    expect(screen.getByLabelText('Budget summary')).toHaveTextContent('USD 1,200.00')
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
      await screen.findByLabelText('Review recommendation'),
    ).toHaveTextContent('Group nearby activities to reduce unnecessary travel.')
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
    expect(screen.getByText('User-provided planning estimate · user-provided allowance')).toBeInTheDocument()
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

  it('waits for a slow status response before scheduling the next poll', async () => {
    vi.useFakeTimers()
    let release!: (value: TripStatusResponse) => void
    vi.mocked(getTripStatus).mockReturnValueOnce(new Promise((resolve) => { release = resolve }))
      .mockResolvedValueOnce(completedStatus)
    vi.mocked(getTrip).mockResolvedValue(trip)
    const view = render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
      expect(getTripStatus).toHaveBeenCalledTimes(1)
      expect(vi.mocked(getTripStatus).mock.calls[0][1]?.aborted).toBe(false)
      await act(async () => { release({ ...completedStatus, status: 'running' }) })
      await act(async () => { await vi.advanceTimersByTimeAsync(3499) })
      expect(getTripStatus).toHaveBeenCalledTimes(1)
      await act(async () => { await vi.advanceTimersByTimeAsync(1) })
      expect(getTripStatus).toHaveBeenCalledTimes(2)
      expect(getTrip).toHaveBeenCalledTimes(1)
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
      expect(getTripStatus).toHaveBeenCalledTimes(2)
    } finally { view.unmount(); vi.useRealTimers() }
  })

  it.each(['planned', 'completed', 'failed', 'revision_required', 'insufficient_data'] as const)(
    'stops the polling loop after terminal status %s and successful details loading', async (status) => {
      vi.useFakeTimers()
      vi.mocked(getTripStatus).mockResolvedValue({ ...completedStatus, status })
      vi.mocked(getTrip).mockResolvedValue({ ...trip, status })
      const view = render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
      try {
        await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
        expect(getTripStatus).toHaveBeenCalledTimes(1)
        expect(getTrip).toHaveBeenCalledTimes(1)
        expect(screen.getByText('Visit the garden')).toBeInTheDocument()
      } finally { view.unmount(); vi.useRealTimers() }
    },
  )

  it('suppresses only lifecycle cancellation through the real API client under StrictMode', async () => {
    const realApi = await vi.importActual<typeof import('../api')>('../api')
    vi.mocked(getTripStatus).mockImplementation(realApi.getTripStatus)
    const signals: AbortSignal[] = []
    let active = 0
    let maximumActive = 0
    const fetchMock = vi.fn().mockImplementation((_url, init: RequestInit) => {
      const signal = init.signal as AbortSignal
      signals.push(signal)
      active += 1
      maximumActive = Math.max(maximumActive, active)
      if (signals.length === 1) return new Promise((_resolve, reject) => {
        signal.addEventListener('abort', () => { active -= 1; reject(new DOMException('Aborted', 'AbortError')) }, { once: true })
      })
      active -= 1
      return Promise.resolve(new Response(JSON.stringify(completedStatus), { status: 200 }))
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.mocked(getTrip).mockResolvedValue(trip)
    const view = render(<StrictMode><TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} /></StrictMode>)
    try {
      await screen.findByText('Visit the garden')
      expect(fetchMock).toHaveBeenCalledTimes(2)
      expect(maximumActive).toBe(1)
      expect(signals[0].aborted).toBe(true)
      expect(signals[1].aborted).toBe(false)
      expect(getTrip).toHaveBeenCalledTimes(1)
      expect(screen.queryByText('The server took too long to respond.')).not.toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    } finally { view.unmount(); vi.unstubAllGlobals() }
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

  it('cancels the StrictMode cleanup poll and loads results from the active poll', async () => {
    vi.mocked(getTripStatus)
      .mockImplementationOnce((_id, signal) => new Promise((_resolve, reject) => {
        signal?.addEventListener('abort', () => reject(new ApiError('The server took too long to respond.')), { once: true })
      }))
      .mockResolvedValueOnce(completedStatus)
    vi.mocked(getTrip).mockResolvedValue(trip)
    render(<StrictMode><TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} /></StrictMode>)
    await screen.findByText('Kyoto, slowly')
    expect(getTripStatus).toHaveBeenCalledTimes(2)
    expect(vi.mocked(getTripStatus).mock.calls[0][1]?.aborted).toBe(true)
    expect(vi.mocked(getTripStatus).mock.calls[1][1]?.aborted).toBe(false)
    expect(getTrip).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('The server took too long to respond.')).not.toBeInTheDocument()
  })

  it('shows a real polling network error and retries successfully', async () => {
    vi.useFakeTimers()
    vi.mocked(getTripStatus)
      .mockRejectedValueOnce(new ApiError('Connection interrupted.'))
      .mockResolvedValueOnce(completedStatus)
    vi.mocked(getTrip).mockResolvedValue(trip)
    const view = render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    try {
      await act(async () => { await Promise.resolve() })
      expect(screen.getByText('Connection interrupted.')).toBeInTheDocument()
      await act(async () => { await vi.advanceTimersByTimeAsync(7000) })
      expect(getTripStatus).toHaveBeenCalledTimes(2)
      expect(screen.getByText('Kyoto, slowly')).toBeInTheDocument()
    } finally { view.unmount(); vi.useRealTimers() }
  })
})

// Fictional backend-shaped fixtures. No live provider or Gemini requests.
function aiDashboardTrip(): TripDetails {
  const scope = { origin: 'Hyderabad', destination: 'Dubai', start_date: '2030-11-10', end_date: '2030-11-14',
    travelers_count: 2, room_count: 1, travel_style: 'balanced', transport_mode: 'flight' as const, currency: 'AED' }
  const rates = [
    { category: 'accommodation', unit: 'room_night', typical: '600.00', quantity: 4, amount: '2400.00' },
    { category: 'intercity_transport', unit: 'traveler_round_trip', typical: '1400.00', quantity: 2, amount: '2800.00' },
    { category: 'local_transport', unit: 'party_day', typical: '90.00', quantity: 5, amount: '450.00' },
    { category: 'food', unit: 'traveler_day', typical: '80.00', quantity: 10, amount: '800.00' },
    { category: 'activities', unit: 'traveler_trip', typical: '230.00', quantity: 2, amount: '460.00' },
  ] as const
  const lineItems: NonNullable<NonNullable<TripDetails['budget']>['line_items']> = rates.map((rate) => ({
    category: rate.category, description: `Planning allowance: ${rate.category}`, amount: rate.amount, currency: 'AED',
    classification: 'estimated', source: 'configured_assumption', evidence_kind: 'planning_estimate', confidence: 'low',
    pricing_basis: 'Validated AI unit rate multiplied by Python trip quantities.', price_sources: [], source_references: [],
    ai_provenance: { method: 'gemini_planning_assumption', model: 'fictional-model', generated_at: '2026-10-14T00:00:00Z', confidence: 'low',
      live_availability_checked: false, scope, quantity: rate.quantity,
      unit_rate: { category: rate.category, unit: rate.unit, typical: rate.typical, currency: 'AED' } },
  }))
  lineItems.push({ category: 'contingency', description: 'Planning reserve', amount: '691.00', currency: 'AED',
    classification: 'estimated', source: 'configured_assumption', evidence_kind: 'planning_estimate', confidence: 'low',
    pricing_basis: 'Python applied 10% to the known subtotal.' })
  const { room_count, transport_mode, ...requestScope } = scope
  return { ...trip, title: 'Dubai at your own pace', currency: 'AED', total_estimated_cost: '7601.00',
    request: { ...trip.request, ...requestScope, total_budget: '8000.00', cost_preferences: { room_count, transport_mode } },
    budget: { currency: 'AED', total_budget: '8000.00', estimated_total: '7601.00', known_cost_subtotal: '7601.00',
      remaining_budget: '399.00', over_budget_amount: '0.00', status: 'WITHIN_BUDGET', pricing_status: 'COMPLETE_ESTIMATE',
      estimate_confidence: 'low', booking_readiness: 'unverified', line_items: lineItems,
      category_totals: lineItems.map((item) => ({ category: item.category!, known_subtotal: item.amount!, currency: 'AED',
        complete: true, included: true, line_item_count: 1 })),
      warnings: ['Costs include low-confidence AI planning assumptions.'], unpriced_components: [],
    },
    optimization_review: { approved: true, review_status: 'APPROVED', budget_status: 'WITHIN_BUDGET',
      qualitative_summary: 'Review the planning allowances before departure.',
      warnings: ['AI planning assumptions are not live market prices.'], issues: [], suggestions: [] },
    days: Array.from({ length: 5 }, (_, index) => ({ day_number: index + 1, itinerary_date: `2030-11-${10 + index}`,
      summary: `Returned plan for day ${index + 1}`, estimated_daily_cost: '1520.20', activities: [{
        ...trip.days[0].activities[0], title: `Returned activity ${index + 1}`, location_name: 'Provider-listed place',
        estimated_cost: null, category: index === 0 ? 'meal_break' : 'sightseeing',
        metadata: { estimated_cost_unknown: true, coordinates_verified: true, latitude: 25.1, longitude: 55.2,
          travel_time_verified: true, travel_time_from_previous_minutes: '12.5',
          warnings: ['Opening hours were not verified.'] },
      }] })),
  }
}

function renderDashboard(result: TripDetails) {
  vi.mocked(getTripStatus).mockResolvedValue({ ...completedStatus, status: result.status })
  vi.mocked(getTrip).mockResolvedValue(result)
  return render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
}

describe('Premium results dashboard', () => {

  it('presents the actual AI flight budget with a clear non-booking disclosure', async () => {
    const result = aiDashboardTrip()
    result.currency = 'INR'
    result.request.currency = 'INR'
    const flight = result.budget!.line_items!.find((item) => item.category === 'intercity_transport')!
    flight.currency = 'INR'
    flight.ai_provenance!.scope.currency = 'INR'
    flight.ai_provenance!.unit_rate.currency = 'INR'
    renderDashboard(result)
    const evidence = await screen.findByRole('region', { name: 'Flight Cost Estimate' })
    expect(within(evidence).getByRole('heading', { name: 'Flight Cost Estimate' })).toBeInTheDocument()
    expect(evidence).toHaveTextContent('INR 2,800.00')
    expect(within(evidence).getByText('AI Estimate')).toBeInTheDocument()
    expect(within(evidence).getByText('Not a verified airline fare. Actual prices may vary.')).toBeVisible()
    expect(within(evidence).getByText('Confidence: Low')).not.toBeVisible()
    expect(within(evidence).getByText('Source: Configured Assumption')).not.toBeVisible()
    expect(within(evidence).getByText('Evidence: Planning Estimate')).not.toBeVisible()
    expect(within(evidence).getByText('Currency: INR')).not.toBeVisible()
    expect(evidence).not.toHaveTextContent('No structured flight reference')
    expect(evidence).not.toHaveTextContent('Verified provider price')
  })

  it.each(['missing_amount', 'unknown', 'wrong_currency', 'wrong_scope', 'wrong_total', 'verified_claim'])('does not simplify invalid AI flight metadata: %s', async (invalid) => {
    const result = aiDashboardTrip()
    const flight = result.budget!.line_items!.find((item) => item.category === 'intercity_transport')!
    if (invalid === 'missing_amount') flight.amount = null
    if (invalid === 'unknown') flight.classification = 'unknown'
    if (invalid === 'wrong_currency') flight.ai_provenance!.unit_rate.currency = 'USD'
    if (invalid === 'wrong_scope') flight.ai_provenance!.scope.start_date = '2030-10-10'
    if (invalid === 'wrong_total') flight.amount = '1.00'
    if (invalid === 'verified_claim') flight.evidence_kind = 'verified_price'
    renderDashboard(result)
    const evidence = await screen.findByLabelText('Flight Cost Estimate')
    expect(screen.getByRole('heading', { name: 'Flight Cost Estimate' })).toBeVisible()
    expect(within(evidence).queryByText('AI Estimate')).not.toBeInTheDocument()
    if (invalid === 'missing_amount') {
      expect(evidence).toHaveTextContent('Flight pricing remains unavailable')
      expect(evidence).not.toHaveTextContent('AED 0.00')
    }
  })

  it('preserves genuine verified flight source and evidence details', async () => {
    const result = aiDashboardTrip()
    result.budget!.line_items = [{ category: 'intercity_transport', description: 'Provider flight quote',
      amount: '2800.00', currency: 'AED', source: 'verified_provider', classification: 'verified',
      evidence_kind: 'verified_price', confidence: 'high',
      source_references: [{ title: 'Airfare provider', provider: 'amadeus', url: 'https://api.amadeus.com/v2/shopping/flight-offers' }],
      price_sources: [{ name: 'Provider quote', observed_on: '2026-10-14', currency: 'AED' }],
    }]
    renderDashboard(result)
    const evidence = await screen.findByLabelText('Flight Cost Estimate')
    expect(evidence).toHaveTextContent('Verified provider price')
    expect(evidence).toHaveTextContent('Provider: Amadeus')
    expect(evidence).toHaveTextContent('Source: Verified Provider')
    expect(evidence).toHaveTextContent('Evidence: Verified Price')
    expect(evidence).toHaveTextContent('Provider quote · Observed: 2026-10-14')
    expect(within(evidence).getByText('Verified provider price', { selector: '.review-pill' })).toBeVisible()
    expect(within(evidence).getByText('Source: Verified Provider')).not.toBeVisible()
    expect(within(evidence).queryByText('Not a verified airline fare. Actual prices may vary.')).not.toBeInTheDocument()
    expect(within(evidence).queryByText('AI Estimate')).not.toBeInTheDocument()
  })

  it('shows the complete AI-assisted plan, original scope, actual status and backend amounts', async () => {
    renderDashboard(aiDashboardTrip())
    expect(await screen.findByRole('heading', { name: 'Your AI Travel Plan Is Ready!' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Dubai' })).toBeInTheDocument()
    expect(screen.getByText('Hyderabad')).toBeInTheDocument()
    expect(screen.getAllByText('Nov 10, 2030').length).toBeGreaterThan(0)
    expect(screen.getByText('Nov 14, 2030', { selector: '.trip-facts time' })).toBeInTheDocument()
    expect(screen.getByText('5 days', { selector: '.trip-facts dd' })).toBeInTheDocument()
    expect(screen.getByText('2 travelers')).toBeInTheDocument()
    expect(screen.getByText('Balanced')).toBeInTheDocument()
    expect(screen.getByLabelText('Plan status')).toHaveTextContent('Completed')
    expect(screen.getByLabelText('Estimated total cost')).toHaveTextContent('AED 7,601.00')
    expect(screen.getByLabelText('Original budget')).toHaveTextContent('AED 8,000.00')
    expect(screen.getByLabelText('Budget balance')).toHaveTextContent('AED 399.00')
    expect(screen.getByRole('meter', { name: 'Estimated budget use' })).toHaveAttribute('aria-valuetext', '95.0% of original budget')
    expect(screen.getByText('Reviewed')).toBeInTheDocument()
    expect(screen.getByLabelText('Optimization summary')).toHaveTextContent('Within budget')
    expect(screen.getByLabelText('Optimization summary')).toHaveTextContent('Booking readinessUnverified')
  })

  it('shows AI unit rates and multipliers as low-confidence non-live assumptions', async () => {
    renderDashboard(aiDashboardTrip())
    const budget = await screen.findByLabelText('Budget summary')
    expect(budget).toHaveTextContent('AI-generated prices are planning estimates, not live booking quotes. Actual costs may vary.')
    expect(within(budget).getAllByText('AI-generated planning estimate · low confidence')).toHaveLength(5)
    expect(budget).toHaveTextContent('AED 1,400.00 per traveler, round trip × 2 units')
    expect(budget).toHaveTextContent('AED 600.00 per room per night × 4 units')
    expect(budget).toHaveTextContent('AED 80.00 per traveler per day × 10 units')
    expect(budget).toHaveTextContent('AED 90.00 per party per day × 5 units')
    expect(budget).toHaveTextContent('AED 230.00 per traveler per trip × 2 units')
    expect(budget).toHaveTextContent('Estimate confidence: Low')
    expect(budget).not.toHaveTextContent('Verified provider price')
    expect(budget).not.toHaveTextContent('Verified-price budget')
    const flight = screen.getByLabelText('Flight Cost Estimate')
    expect(flight).toHaveTextContent('AI-generated planning estimate')
    expect(flight).not.toHaveTextContent('Provider:')
    expect(flight).not.toHaveTextContent('Retrieved:')
  })

  it('displays backend category totals and accessible visual comparisons without summing a new total', async () => {
    renderDashboard(aiDashboardTrip())
    const categories = await screen.findByLabelText('Category cost breakdown')
    for (const label of ['Accommodation', 'Round-trip intercity travel', 'Local transportation', 'Food', 'Activities', 'Contingency']) {
      expect(within(categories).getByText(label)).toBeInTheDocument()
    }
    const bar = within(categories).getByRole('meter', { name: 'Accommodation share of original budget' })
    expect(bar).toHaveAttribute('aria-valuenow', '30')
    expect(bar.firstElementChild).toHaveStyle({ width: '30%' })
    expect(categories).toHaveTextContent('AED 2,400.00')
    expect(categories).toHaveTextContent('AED 691.00')
  })

  it('keeps all returned itinerary days, meal classifications, prices and POI evidence faithful', async () => {
    renderDashboard(aiDashboardTrip())
    const itinerary = await screen.findByRole('region', { name: 'Day-by-day itinerary' })
    expect(within(itinerary).getAllByRole('article')).toHaveLength(5)
    const day = within(itinerary).getByRole('article', { name: 'Day 1 itinerary' })
    expect(day).toHaveTextContent('Nov 10, 2030')
    expect(day).toHaveTextContent('Returned activity 1')
    expect(day).toHaveTextContent('Meal Break')
    expect(day).toHaveTextContent('Verified location · 25.1000, 55.2000')
    expect(day).toHaveTextContent('Verified travel time from previous stop · 12.5 min')
    expect(day).not.toHaveTextContent('Price not available')
    expect(day).toHaveTextContent('Opening hours were not verified.')
    expect(day).not.toHaveTextContent('AED 0.00')
    expect(day).not.toHaveTextContent('Attraction')
    expect(screen.getByRole('region', { name: 'Planner review' })).toHaveTextContent('AI planning assumptions are not live market prices.')
    expect(screen.getByLabelText('Trip warnings')).toHaveTextContent('Costs include low-confidence AI planning assumptions.')
  })

  it('makes an incomplete itinerary available without a fictional total, balance, approval or flight cost', async () => {
    const result = aiDashboardTrip()
    result.status = 'insufficient_data'
    result.total_estimated_cost = null
    Object.assign(result.budget!, { estimated_total: null, remaining_budget: null, status: 'INCOMPLETE_ESTIMATE', pricing_status: 'INCOMPLETE',
      known_cost_subtotal: '4801.00', unpriced_components: ['intercity_transport'], missing_price_inputs: ['intercity_round_trip_fare_per_traveler'],
      missing_price_actions: ['intercity_transport: supply an applicable round-trip fare or a user allowance.'] })
    const flight = result.budget!.line_items!.find((item) => item.category === 'intercity_transport')!
    Object.assign(flight, { amount: null, ai_provenance: null, source: 'unknown', classification: 'unknown' })
    Object.assign(result.budget!.category_totals!.find((item) => item.category === 'intercity_transport')!, { complete: false, known_subtotal: '0.00' })
    result.optimization_review = { approved: false, review_status: 'INSUFFICIENT_DATA' }
    renderDashboard(result)
    expect(await screen.findByRole('heading', { name: 'Your Personalized Itinerary Is Ready' })).toBeInTheDocument()
    expect(screen.getByLabelText('Estimated total cost')).toHaveTextContent('Not available')
    expect(screen.getByLabelText('Known-cost subtotal')).toHaveTextContent('Incomplete subtotalAED 4,801.00')
    expect(screen.getByLabelText('Budget balance')).toHaveTextContent('Not available')
    expect(screen.queryByRole('meter', { name: 'Estimated budget use' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('Flight Cost Estimate')).toHaveTextContent('Flight pricing remains unavailable')
    expect(screen.getByLabelText('Missing information')).toHaveTextContent('Round-trip transport per traveler')
    expect(screen.getByLabelText('Budget summary')).not.toHaveTextContent('AED 0.00')
    expect(screen.queryByText('Reviewed')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Optimization summary')).toHaveTextContent('Affordability unconfirmed')
    expect(screen.getByLabelText('Day-by-day itinerary')).toHaveTextContent('Returned activity 5')
    expect(screen.getByLabelText('Day-by-day itinerary').compareDocumentPosition(screen.getByLabelText('Missing information')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('shows an over-budget plan accurately without claiming optimization succeeded', async () => {
    const result = aiDashboardTrip()
    result.status = 'revision_required'
    result.request.total_budget = '7000.00'
    Object.assign(result.budget!, { total_budget: '7000.00', remaining_budget: '-601.00', over_budget_amount: '601.00', status: 'OVER_BUDGET' })
    result.optimization_review = { approved: false, review_status: 'REVISION_REQUIRED', warnings: ['No supported cheaper alternative is available.'],
      issues: [{ description: 'Complete estimate exceeds the original budget.', blocks_approval: true, recommended_action: 'Review mandatory constraints with the traveler.' }] }
    renderDashboard(result)
    expect(await screen.findByRole('heading', { name: 'Your Travel Plan Needs Budget Optimization' })).toBeInTheDocument()
    expect(screen.getByLabelText('Budget balance')).toHaveTextContent('AMOUNT OVER BUDGETAED 601.00')
    expect(screen.getByRole('meter', { name: 'Estimated budget use' })).toHaveAttribute('aria-valuetext', '108.6% of original budget')
    expect(screen.getByRole('meter', { name: 'Estimated budget use' })).toHaveAttribute('aria-valuenow', '100')
    expect(screen.getByLabelText('Planner review')).toHaveTextContent('No supported cheaper alternative is available.')
    expect(screen.getByLabelText('Planner review')).toHaveTextContent('Review mandatory constraints with the traveler.')
    expect(screen.getByLabelText('Optimization summary')).toHaveTextContent('Revision history was not supplied')
    expect(screen.getByLabelText('Optimization summary')).not.toHaveTextContent('Within budget')
    expect(screen.queryByText(/optimization succeeded|saved AED/i)).not.toBeInTheDocument()
    expect(screen.queryByText('Reviewed')).not.toBeInTheDocument()
  })

  it('reports an actual backend revision warning without inventing a revision count or achieved savings', async () => {
    const result = aiDashboardTrip()
    result.warnings = ['Applied a validated itinerary-only revision; original budget assumptions and total were preserved.']
    renderDashboard(result)
    const summary = await screen.findByLabelText('Optimization summary')
    expect(summary).toHaveTextContent('Revision attempt recorded by the planner')
    expect(within(summary).getByLabelText('Returned revision results')).toHaveTextContent(result.warnings[0])
    expect(summary).not.toHaveTextContent('Supported potential savings')
    expect(summary).not.toHaveTextContent('Revision 1')
  })

  it.each([false, true])('shows backend savings only with matching evidence (supported=%s)', async (supported) => {
    const result = aiDashboardTrip()
    Object.assign(result.optimization_review!, { estimated_savings: '100.00', currency: 'AED', suggestions: [{
      category: 'budget', recommendation: 'Remove the optional extra.', estimated_savings: '100.00', currency: 'AED',
      savings_basis: supported ? 'Remove a priced optional expense from the supplied snapshot.' : null,
    }] })
    renderDashboard(result)
    const summary = await screen.findByLabelText('Optimization summary')
    expect(summary.textContent?.includes('Supported potential savings:')).toBe(supported)
    expect(summary).not.toHaveTextContent('Saved AED')
    if (supported) {
      expect(summary).toHaveTextContent('AED 100.00')
      expect(summary).toHaveTextContent('not a confirmed reduction from a previous plan')
    }
  })

  it.each(['NaN', 'Infinity', '-10', ''])('never presents invalid total %s as a complete estimate', async (amount) => {
    const result = aiDashboardTrip()
    result.budget!.estimated_total = amount
    result.total_estimated_cost = amount
    renderDashboard(result)
    await screen.findByLabelText('Budget summary')
    expect(screen.getByLabelText('Estimated total cost')).toHaveTextContent('Not available')
    expect(screen.queryByRole('meter', { name: 'Estimated budget use' })).not.toBeInTheDocument()
    expect(screen.queryByText('Reviewed')).not.toBeInTheDocument()
  })

  it('preserves reviewer warnings and blocks approval when the backend has returned inconsistent reviewer flags', async () => {
    const result = aiDashboardTrip()
    result.optimization_review!.issues = [{ description: 'Mandatory attraction cannot be verified.', blocks_approval: true }]
    renderDashboard(result)
    await screen.findByText('Returned activity 1')
    expect(screen.getByText('Review needed')).toBeInTheDocument()
    expect(screen.getByLabelText('Planner review')).toHaveTextContent('Reviewer approval is not confirmed.')
    expect(screen.getByLabelText('Planner review')).toHaveTextContent('Mandatory attraction cannot be verified.')
    expect(screen.getByLabelText('Optimization summary')).toHaveTextContent('Approval not confirmed')
  })

  it.each(['subtotal', 'balance', 'ledger', 'currency'])('does not infer a total or approval from conflicting saved %s', async (conflict) => {
    const result = aiDashboardTrip()
    if (conflict === 'subtotal') result.budget!.known_cost_subtotal = '7500.00'
    if (conflict === 'balance') result.budget!.remaining_budget = '500.00'
    if (conflict === 'ledger') result.budget!.line_items![0].amount = '1.00'
    if (conflict === 'currency') result.budget!.currency = result.currency = 'USD'
    renderDashboard(result)
    await screen.findByLabelText('Budget summary')
    expect(screen.getByLabelText('Estimated total cost')).toHaveTextContent('Not available')
    expect(screen.getByLabelText('Original budget')).toHaveTextContent('AED 8,000.00')
    expect(screen.getByLabelText('Budget balance')).toHaveTextContent('Not available')
    expect(screen.getByLabelText('Planner review')).toHaveTextContent('Reviewer approval is not confirmed.')
    expect(screen.queryByText('Reviewed')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Budget summary')).toHaveTextContent(conflict === 'currency'
      ? 'no conversion has been inferred' : 'A corrected total has not been inferred')
  })
})

function overBudgetResult(): TripDetails {
  return { ...trip, status: 'revision_required', total_estimated_cost: '1200.00',
    request: { ...trip.request, total_budget: '1000.00' },
    budget: { currency: 'USD', total_budget: '1000.00', estimated_total: '1200.00', known_cost_subtotal: '1200.00',
      remaining_budget: '-200.00', over_budget_amount: '200.00', status: 'OVER_BUDGET', pricing_status: 'COMPLETE_ESTIMATE',
      booking_readiness: 'unverified', line_items: [], category_totals: [], unpriced_components: [] },
    optimization_review: { approved: false, review_status: 'REVISION_REQUIRED' },
  }
}

describe('Interactive budget revision', () => {

  it('preserves the initial revision-required badge without a completed saved revision', async () => {
    const result = overBudgetResult()
    renderDashboard(result)
    expect(await screen.findByLabelText('Plan status')).toHaveTextContent(/^Revision Required$/)
    expect(screen.queryByText('Review Completed', { exact: true })).not.toBeInTheDocument()
    expect(result.status).toBe('revision_required')
  })

  it('queues one revision, polls and refreshes the actual saved result', async () => {
    const original = overBudgetResult()
    const final: TripDetails = { ...original, status: 'completed', total_estimated_cost: '900.00',
      budget: { ...original.budget, status: 'WITHIN_BUDGET', estimated_total: '900.00', known_cost_subtotal: '900.00',
        remaining_budget: '100.00', over_budget_amount: '0.00' },
      optimization_review: { approved: true, review_status: 'APPROVED' },
      budget_revision_summary: { attempts: 1, max_attempts: 2, can_retry: true, history: [{ attempt: 1,
        original_total: '1200.00', revised_total: '900.00', savings: '300.00', remaining_over_budget: '0.00',
        currency: 'USD', outcome: 'within_budget', changes: ['Removed a priced optional excursion.'],
        explanation: 'Supported changes brought the estimate within the original budget.' }] },
    }
    vi.mocked(getTripStatus).mockResolvedValueOnce({ ...completedStatus, status: 'revision_required' })
      .mockResolvedValueOnce({ ...completedStatus, status: 'running', current_stage: 'budget_revision', progress_percentage: 60,
        message: 'Checking supported budget choices.' }).mockResolvedValue({ ...completedStatus, status: 'completed' })
    vi.mocked(getTrip).mockResolvedValueOnce(original).mockResolvedValue(final)
    let accept!: (value: Awaited<ReturnType<typeof reviseTrip>>) => void
    vi.mocked(reviseTrip).mockReturnValue(new Promise((resolve) => { accept = resolve }))
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    const button = await screen.findByRole('button', { name: 'Revise Plan to Fit Budget' })
    expect(screen.getByLabelText('Revise budget')).toHaveTextContent('USD 200.00')
    act(() => { button.click(); button.click() })
    expect(reviseTrip).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'Optimizing your travel plan...' })).toBeDisabled()
    await act(async () => { accept({ trip_id: 'trip-123', task_id: 'trip-123', status: 'queued', message: 'Optimizing your travel plan...' }) })
    expect(await screen.findByLabelText('Current revision')).toHaveTextContent('Revising budget')
    expect(screen.getByLabelText('Planning status message')).toHaveTextContent('Checking supported budget choices.')
    const history = await screen.findByLabelText('Budget revision results', {}, { timeout: 5000 })
    vi.useFakeTimers()
    try {
      expect(history).toHaveTextContent('Before: USD 1,200.00 · After: USD 900.00')
      expect(history).toHaveTextContent('Actual estimate reduction: USD 300.00')
      expect(history).toHaveTextContent('Removed a priced optional excursion.')
      expect(screen.getByLabelText('Original budget')).toHaveTextContent('USD 1,000.00')
      expect(screen.queryByRole('button', { name: 'Revise Plan to Fit Budget' })).not.toBeInTheDocument()
      expect(screen.getByText('Reviewed')).toBeInTheDocument()
      const polls = vi.mocked(getTripStatus).mock.calls.length
      await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
      expect(getTripStatus).toHaveBeenCalledTimes(polls)
    } finally { vi.useRealTimers() }
  })

  it('surfaces a failed mutation without automatic duplicate POSTs', async () => {
    renderDashboard(overBudgetResult())
    vi.mocked(reviseTrip).mockRejectedValue(new ApiError('No new permitted alternatives are available.', 409))
    const button = await screen.findByRole('button', { name: 'Revise Plan to Fit Budget' })
    act(() => button.click())
    expect(await screen.findByText('No new permitted alternatives are available.')).toBeInTheDocument()
    expect(reviseTrip).toHaveBeenCalledTimes(1)
    expect(await screen.findByRole('button', { name: 'Revise Plan to Fit Budget' })).toBeEnabled()
  })

  it('restores persisted no-savings history and requires new permission before retry', async () => {
    const result = overBudgetResult()
    result.budget_revision_summary = { attempts: 1, max_attempts: 2, can_retry: true, requires_economy_permission: true,
      history: [{ attempt: 1, original_total: '1200.00', revised_total: '1200.00', savings: '0.00',
        remaining_over_budget: '200.00', currency: 'USD', outcome: 'no_supported_alternative', changes: [],
        explanation: 'No further supported reduction was found.' }] }
    renderDashboard(result)
    const button = await screen.findByRole('button', { name: 'Revise Plan to Fit Budget' })
    expect(button).toBeDisabled()
    expect(screen.getByLabelText('Budget revision results')).toHaveTextContent('Still over budget: USD 200.00')
    expect(screen.getByLabelText('Budget revision results')).toHaveTextContent('No further supported reduction was found.')
    expect(screen.getByLabelText('Optimization summary')).toHaveTextContent('1 interactive revision attempt(s) recorded; see the before-and-after results.')
    expect(screen.queryByText('Reviewed')).not.toBeInTheDocument()
    act(() => screen.getByRole('checkbox', { name: /Allow economy accommodation/ }).click())
    expect(button).toBeEnabled()
    vi.mocked(reviseTrip).mockRejectedValue(new ApiError('Unavailable', 503))
    act(() => button.click())
    await screen.findByText('Unavailable')
    expect(reviseTrip).toHaveBeenCalledWith('trip-123', true)
  })

  it.each(['affordable', 'missing_total', 'missing_category', 'currency_mismatch'])('hides revision for %s plans', async (kind) => {
    const result = kind === 'affordable' ? aiDashboardTrip() : overBudgetResult()
    if (kind === 'missing_total') result.budget!.estimated_total = null
    if (kind === 'missing_category') result.budget!.unpriced_components = ['intercity_transport']
    if (kind === 'currency_mismatch') result.budget!.currency = 'INR'
    renderDashboard(result)
    await screen.findByLabelText('Budget summary')
    expect(screen.queryByRole('button', { name: 'Revise Plan to Fit Budget' })).not.toBeInTheDocument()
  })

  it('disables further attempts when the server reports the limit reached', async () => {
    const result = overBudgetResult()
    result.budget_revision_summary = { attempts: 2, max_attempts: 2, can_retry: false, history: [] }
    renderDashboard(result)
    expect(await screen.findByRole('button', { name: 'Revise Plan to Fit Budget' })).toBeDisabled()
    expect(screen.getByText(/No further revision is eligible/)).toBeInTheDocument()
    expect(reviseTrip).not.toHaveBeenCalled()
  })
})


describe('Compact result cards', () => {
  it('limits visible travel notes to two concise messages and preserves deduplicated details', async () => {
    const result = overBudgetResult()
    result.warnings = ['Internal provider diagnostic repeated.', 'Internal provider diagnostic repeated.',
      'A long technical route explanation which must remain available only on request.']
    result.errors = ['The saved review still requires changes.']
    renderDashboard(result)
    const card = await screen.findByLabelText('Trip warnings')
    const notes = within(card).getByLabelText('Important travel notes')
    expect(notes.querySelectorAll('p')).toHaveLength(2)
    expect(notes).toHaveTextContent('This plan is over budget by USD 200.00.')
    expect(notes).toHaveTextContent('Booking prices and availability are unverified.')
    expect(notes).not.toHaveTextContent('Internal provider')
    const diagnostic = within(card).getByText('Internal provider diagnostic repeated.')
    expect(diagnostic).not.toBeVisible()
    expect(within(card).getAllByText('Internal provider diagnostic repeated.')).toHaveLength(1)
    act(() => within(card).getByText('View details').click())
    expect(diagnostic).toBeVisible()
  })

  it('keeps incomplete-cost uncertainty visible without exposing technical warnings', async () => {
    const result = overBudgetResult()
    result.status = 'insufficient_data'
    result.budget!.estimated_total = null
    result.budget!.pricing_status = 'INCOMPLETE'
    result.budget!.unpriced_components = ['intercity_transport']
    result.warnings = ['Flight pricing adapter returned no exact-date passenger-matched reference.']
    renderDashboard(result)
    const card = await screen.findByLabelText('Trip warnings')
    expect(within(card).getByText('Some required costs or trip details are missing. This plan is incomplete.')).toBeVisible()
    expect(within(card).getByText(result.warnings[0])).not.toBeVisible()
    expect(screen.queryByRole('button', { name: 'Revise Plan to Fit Budget' })).not.toBeInTheDocument()
  })

  it('shows approval, overrun and one short recommendation while preserving the complete review', async () => {
    const result = overBudgetResult()
    result.optimization_review = { approved: false, review_status: 'REVISION_REQUIRED',
      qualitative_summary: 'The complete planning estimate exceeds the original budget.',
      warnings: ['Detailed reviewer warning.'],
      issues: [{ description: 'The total exceeds the hard budget.', blocks_approval: true,
        evidence_basis: 'Deterministic budget reconciliation.', recommended_action: 'Compare supported lower-cost options. Keep the original budget unchanged.' }],
      suggestions: [{ category: 'accommodation', recommendation: 'Another detailed accommodation recommendation.' }],
    }
    renderDashboard(result)
    const card = await screen.findByLabelText('Planner review')
    expect(within(card).getByText('Reviewer approval is not confirmed.')).toBeVisible()
    expect(within(card).getByText('Over budget:', { exact: false, selector: '.compact-overrun' })).toHaveTextContent('USD 200.00')
    expect(within(card).getByLabelText('Review recommendation')).toHaveTextContent('Compare supported lower-cost options.')
    expect(within(card).getByLabelText('Review recommendation')).not.toHaveTextContent('Keep the original budget')
    expect(within(card).getByLabelText('Reviewer summary')).not.toBeVisible()
    expect(within(card).getByText('Detailed reviewer warning.')).not.toBeVisible()
    expect(within(card).getByText('Evidence: Deterministic budget reconciliation.')).not.toBeVisible()
    expect(screen.getByRole('button', { name: 'Revise Plan to Fit Budget' })).toBeEnabled()
    act(() => within(card).getByText('View details').click())
    expect(within(card).getByText('Detailed reviewer warning.')).toBeVisible()
    expect(within(card).getByText('Another detailed accommodation recommendation.')).toBeVisible()
  })

  it('keeps AI flight amounts prominent and provenance collapsed', async () => {
    renderDashboard(aiDashboardTrip())
    const card = await screen.findByLabelText('Flight Cost Estimate')
    expect(within(card).getByText('AED 2,800.00')).toBeVisible()
    expect(within(card).getByText('AI Estimate')).toBeVisible()
    expect(within(card).getByText('Not a verified airline fare. Actual prices may vary.')).toBeVisible()
    expect(within(card).getByText('Source: Configured Assumption')).not.toBeVisible()
    expect(within(card).getByText('Confidence: Low')).not.toBeVisible()
    act(() => within(card).getByText('View pricing details').click())
    expect(within(card).getByText('Source: Configured Assumption')).toBeVisible()
    expect(within(card).getByText('Confidence: Low')).toBeVisible()
    expect(card).toHaveTextContent('AED 1,400.00 per traveler, round trip × 2 units')
  })

  it('keeps cached fare route and freshness available on expansion without a live claim', async () => {
    renderDashboard(cachedFlightTrip(flightReference()))
    const card = await screen.findByLabelText('Flight Cost Estimate')
    expect(within(card).getByText('Cached / reference estimate')).toBeVisible()
    const route = within(card).getByText('Route:', { exact: false })
    expect(route).not.toBeVisible()
    expect(within(card).getByText('Not a verified airline fare. Actual prices may vary.')).toBeVisible()
    act(() => within(card).getByText('View pricing details').click())
    expect(route).toBeVisible()
    expect(within(card).getByText('Retrieved:', { exact: false })).toBeVisible()
    expect(card).not.toHaveTextContent('Verified provider price')
  })

  it('never fills a missing flight price with zero or an AI label', async () => {
    const result = overBudgetResult()
    result.budget!.line_items = [{ category: 'intercity_transport', amount: null, currency: 'USD', source: 'unknown', classification: 'unknown' }]
    renderDashboard(result)
    const card = await screen.findByLabelText('Flight Cost Estimate')
    expect(within(card).getByText('No applicable flight price is included. Flight pricing remains unavailable.')).toBeVisible()
    expect(card.querySelector('.flight-cost-summary')).toBeNull()
    expect(card).not.toHaveTextContent('USD 0.00')
    expect(within(card).queryByText('AI Estimate')).not.toBeInTheDocument()
  })
})


describe('Final professional revision outcome', () => {
  function outcomeTrip(outcome: string, changes: Partial<TripDetails> = {}): TripDetails {
    const result = overBudgetResult()
    result.budget_revision_summary = { attempts: 1, max_attempts: 5, can_retry: false, economy_accommodation_available: false,
      history: [{ attempt: 1, original_total: '1500.00', revised_total: '1200.00', savings: '300.00', cumulative_savings: '300.00',
        original_budget: '1000.00', remaining_over_budget: '200.00', currency: 'USD', outcome,
        alternatives_exhausted: true, changes: ['accommodation: 600.00 → 300.00 USD'],
        explanation: 'Supported costs were reduced; new prices or approved changes are needed to close the remaining gap.' }] }
    return { ...result, ...changes }
  }

  it('shows the successful outcome and all four supplied summary values', async () => {
    const result = outcomeTrip('within_budget')
    result.status = 'completed'
    result.optimization_review = { approved: true, review_status: 'APPROVED' }
    result.total_estimated_cost = '900.00'
    Object.assign(result.budget!, { estimated_total: '900.00', known_cost_subtotal: '900.00', remaining_budget: '100.00',
      over_budget_amount: '0.00', status: 'WITHIN_BUDGET' })
    Object.assign(result.budget_revision_summary!.history[0], { revised_total: '900.00', savings: '600.00', cumulative_savings: '600.00', remaining_over_budget: '0.00' })
    renderDashboard(result)
    const card = await screen.findByLabelText('Budget revision results')
    expect(screen.getByRole('heading', { name: 'Budget Optimized Successfully' })).toBeVisible()
    expect(screen.getByLabelText('Plan status')).toHaveTextContent(/^Approved$/)
    expect(result.status).toBe('completed')
    expect(within(card).getByText('Original budget').nextElementSibling).toHaveTextContent('USD 1,000.00')
    expect(within(card).getByText('Final estimated cost').nextElementSibling).toHaveTextContent('USD 900.00')
    expect(within(card).getByText('Actual savings achieved').nextElementSibling).toHaveTextContent('USD 600.00')
    expect(within(card).getByText('Remaining overrun').nextElementSibling).toHaveTextContent('USD 0.00')
    expect(card).toHaveTextContent('Booking readiness remains unverified. AI estimates are not live fares.')
    expect(screen.getByText('Reviewed')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Revise Plan to Fit Budget' })).not.toBeInTheDocument()
  })

  it('shows partial savings with a remaining gap and disables an exhausted revision', async () => {
    renderDashboard(outcomeTrip('improved'))
    const card = await screen.findByLabelText('Budget revision results')
    expect(screen.getByRole('heading', { name: 'Budget Review Completed' })).toBeVisible()
    expect(screen.getByRole('heading', { name: 'Best Supported Plan — Over Budget' })).toBeVisible()
    expect(screen.getByLabelText('Plan status')).toHaveTextContent('Review Completed')
    expect(screen.getByText('Over Budget', { exact: true })).toBeVisible()
    expect(screen.getByLabelText('Plan status')).not.toHaveTextContent('Revision Required')
    expect(screen.queryByText('Your plan needs a few adjustments.')).not.toBeInTheDocument()
    expect(screen.queryByText('Your Travel Plan Needs Budget Optimization')).not.toBeInTheDocument()
    expect(within(card).getByText('Actual savings achieved').nextElementSibling).toHaveTextContent('USD 300.00')
    expect(within(card).getByText('Remaining overrun').nextElementSibling).toHaveTextContent('USD 200.00')
    expect(screen.getByRole('button', { name: 'Revise Plan to Fit Budget' })).toBeDisabled()
    expect(screen.queryByRole('checkbox', { name: /Allow economy accommodation/ })).not.toBeInTheDocument()
    const evidence = within(card).getByText('accommodation: 600.00 → 300.00 USD')
    expect(evidence).not.toBeVisible()
    act(() => within(card).getByText('View revision details').click())
    expect(evidence).toBeVisible()
    expect(screen.queryByText('Reviewed')).not.toBeInTheDocument()
  })

  it('retains cumulative savings after a final no-op without claiming global infeasibility', async () => {
    const result = outcomeTrip('no_supported_alternative')
    const previous = { ...result.budget_revision_summary!.history[0], outcome: 'improved', savings: '300.00' }
    Object.assign(result.budget_revision_summary!.history[0], { attempt: 2, original_total: '1200.00', savings: '0.00' })
    result.budget_revision_summary!.history.unshift(previous)
    result.budget_revision_summary!.attempts = 2
    renderDashboard(result)
    const card = await screen.findByLabelText('Budget revision results')
    expect(within(card).getByLabelText('Revision outcome explanation')).toHaveTextContent('No further compatible savings available with current pricing information.')
    expect(screen.getByRole('heading', { name: 'Budget Review Completed' })).toBeVisible()
    expect(screen.getByLabelText('Plan status')).toHaveTextContent('Review Completed')
    expect(screen.getByText('Over Budget', { exact: true })).toBeVisible()
    expect(result.status).toBe('revision_required')
    expect(within(card).getByText('Actual savings achieved').nextElementSibling).toHaveTextContent('USD 300.00')
    const button = screen.getByRole('button', { name: 'Revise Plan to Fit Budget' })
    act(() => { button.click(); button.click() })
    expect(reviseTrip).not.toHaveBeenCalled()
    expect(card).not.toHaveTextContent('impossible')
  })

  it('keeps incomplete pricing distinct and never shows a false total or zero savings', async () => {
    const result = outcomeTrip('within_budget')
    result.status = 'insufficient_data'
    Object.assign(result.budget!, { estimated_total: null, pricing_status: 'INCOMPLETE', unpriced_components: ['intercity_transport'] })
    renderDashboard(result)
    const card = await screen.findByLabelText('Budget revision results')
    expect(screen.getByRole('heading', { name: 'Budget review is incomplete' })).toBeVisible()
    expect(within(card).getByLabelText('Revision outcome explanation')).toHaveTextContent('Required costs or applicable evidence are missing')
    expect(within(card).getByText('Final estimated cost').nextElementSibling).toHaveTextContent('Not available')
    expect(within(card).getByText('Actual savings achieved').nextElementSibling).toHaveTextContent('Not available')
    expect(within(card).getByText('Remaining overrun').nextElementSibling).toHaveTextContent('Not available')
    expect(screen.queryByRole('heading', { name: 'Budget Optimized Successfully' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Revise Plan to Fit Budget' })).not.toBeInTheDocument()
  })

  it('offers review options without silently changing constraints or posting a revision', async () => {
    vi.mocked(getTripStatus).mockResolvedValue({ ...completedStatus, status: 'revision_required' })
    vi.mocked(getTrip).mockResolvedValue(outcomeTrip('improved'))
    const reviewOptions = vi.fn()
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={reviewOptions} />)
    const button = await screen.findByRole('button', { name: 'Review budget or add prices' })
    expect(screen.getByLabelText('Revision next steps')).toHaveTextContent('Review a higher budget or supply new supported travel prices.')
    expect(screen.getByLabelText('Revision next steps')).toHaveTextContent('Changes require your review and a new submission.')
    act(() => button.click())
    expect(reviewOptions).toHaveBeenCalledTimes(1)
    expect(reviseTrip).not.toHaveBeenCalled()
    expect(screen.getByLabelText('Original budget')).toHaveTextContent('USD 1,000.00')
  })

  it('requires explicit accommodation permission for the only meaningful retry', async () => {
    const result = outcomeTrip('improved')
    Object.assign(result.budget_revision_summary!, { can_retry: true, requires_economy_permission: true, economy_accommodation_available: true })
    renderDashboard(result)
    const button = await screen.findByRole('button', { name: 'Revise Plan to Fit Budget' })
    expect(button).toBeDisabled()
    expect(screen.getByLabelText('Revision next steps')).toHaveTextContent('Approve economy accommodation')
    act(() => screen.getByRole('checkbox', { name: /Allow economy accommodation/ }).click())
    expect(button).toBeEnabled()
    expect(reviseTrip).not.toHaveBeenCalled()
  })

  it('shows a saved ongoing revision as progress, even before any result details load', async () => {
    vi.mocked(getTripStatus).mockResolvedValue({ ...completedStatus, status: 'running', current_stage: 'budget_revision',
      progress_percentage: 55, message: 'Evaluating compatible accommodation choices.' })
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    expect(await screen.findByRole('heading', { name: 'Optimizing your travel plan...' })).toBeVisible()
    expect(screen.getByLabelText('Plan status')).toHaveTextContent(/^Optimizing$/)
    expect(screen.getByLabelText('Planning status message')).toHaveTextContent('Evaluating compatible accommodation choices.')
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '55')
    expect(screen.queryByText('Budget Review Completed')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Budget revision results')).not.toBeInTheDocument()
    expect(getTrip).not.toHaveBeenCalled()
    expect(getTripStatus).toHaveBeenCalledTimes(1)
  })

  it('does not claim exhaustion just because a retry limit was reached', async () => {
    const result = outcomeTrip('improved')
    result.budget_revision_summary!.history[0].alternatives_exhausted = false
    renderDashboard(result)
    expect(await screen.findByRole('heading', { name: 'Best Supported Plan — Over Budget' })).toBeVisible()
    expect(screen.getByLabelText('Revision outcome explanation')).toHaveTextContent('Supported alternatives were evaluated')
    expect(screen.getByLabelText('Plan status')).toHaveTextContent('Review Completed')
    expect(screen.getByText('Over Budget', { exact: true })).toBeVisible()
    expect(screen.getByLabelText('Revision outcome explanation')).not.toHaveTextContent('No further compatible savings')
    expect(screen.getByRole('button', { name: 'Revise Plan to Fit Budget' })).toBeDisabled()
  })

  it('shows a failed saved revision and its error without a completion or exhaustion claim', async () => {
    const result = outcomeTrip('failed', { status: 'failed' })
    vi.mocked(getTripStatus).mockResolvedValue({ ...completedStatus, status: 'failed', error_message: 'The revision deadline was exceeded.' })
    vi.mocked(getTrip).mockResolvedValue(result)
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    expect(await screen.findByRole('heading', { name: 'Budget Revision Failed' })).toBeVisible()
    expect(screen.getByText('The revision deadline was exceeded.')).toBeVisible()
    expect(screen.getByLabelText('Plan status')).toHaveTextContent(/^Failed$/)
    expect(screen.queryByText('Budget Review Completed')).not.toBeInTheDocument()
    expect(screen.queryByText('Best Supported Plan — Over Budget')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Revision outcome explanation')).not.toHaveTextContent('No further compatible savings')
    expect(screen.getByLabelText('Budget revision results')).toHaveTextContent('The revision could not be completed.')
    expect(screen.getByRole('button', { name: 'Review budget or add prices' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Try another plan' })).not.toBeInTheDocument()
  })

  it.each([
    null,
    { approved: false, review_status: 'REVISION_REQUIRED' as const },
    { approved: true, review_status: 'APPROVED' as const, issues: [{ description: 'A mandatory constraint failed.', blocks_approval: true }] },
  ])('requires explicit valid reviewer approval before labeling a revision successful: %j', async (review) => {
    const result = outcomeTrip('within_budget', { status: 'completed', total_estimated_cost: '900.00', optimization_review: review })
    Object.assign(result.budget!, { estimated_total: '900.00', known_cost_subtotal: '900.00', remaining_budget: '100.00',
      over_budget_amount: '0.00', status: 'WITHIN_BUDGET' })
    result.budget_revision_summary!.history[0].revised_total = '900.00'
    renderDashboard(result)
    expect(await screen.findByRole('heading', { name: 'Budget Review Completed' })).toBeVisible()
    expect(screen.queryByRole('heading', { name: 'Budget Optimized Successfully' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('Revision outcome explanation')).toHaveTextContent('reviewer approval is not confirmed')
    expect(screen.getByLabelText('Plan status')).toHaveTextContent(/^Completed$/)
    expect(screen.getByText('Visit the garden')).toBeVisible()
  })

  it.each(['unknown_outcome', 'stale_total', 'missing_history', 'pending_attempt'])('does not invent an evaluated or successful outcome from %s', async (kind) => {
    const result = outcomeTrip('improved')
    if (kind === 'unknown_outcome') result.budget_revision_summary!.history[0].outcome = 'unknown'
    if (kind === 'stale_total') result.budget_revision_summary!.history[0].revised_total = '1100.00'
    if (kind === 'missing_history') result.budget_revision_summary!.history = []
    if (kind === 'pending_attempt') result.budget_revision_summary!.attempts = 2
    renderDashboard(result)
    expect(await screen.findByRole('heading', { name: 'Revision Outcome Unavailable' })).toBeVisible()
    expect(screen.getByLabelText('Plan status')).toHaveTextContent(/^Revision Required$/)
    expect(screen.queryByRole('heading', { name: 'Best Supported Plan — Over Budget' })).not.toBeInTheDocument()
    expect(screen.queryByText('Budget Review Completed')).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Budget Optimized Successfully' })).not.toBeInTheDocument()
    expect(screen.queryByText('Your plan needs a few adjustments.')).not.toBeInTheDocument()
    expect(screen.getByText('Visit the garden')).toBeVisible()
  })

  it('waits for final saved details after 202 instead of displaying an older revision as completed', async () => {
    const previous = outcomeTrip('improved')
    previous.budget_revision_summary!.can_retry = true
    previous.budget_revision_summary!.history[0].alternatives_exhausted = false
    const final = outcomeTrip('no_supported_alternative')
    let finishDetails!: (value: TripDetails) => void
    vi.mocked(getTripStatus).mockResolvedValue({ ...completedStatus, status: 'revision_required' })
    vi.mocked(getTrip).mockResolvedValueOnce(previous).mockReturnValueOnce(new Promise((resolve) => { finishDetails = resolve }))
    vi.mocked(reviseTrip).mockResolvedValue({ trip_id: 'trip-123', task_id: 'trip-123', status: 'queued', message: 'Revision accepted.' })
    render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
    const button = await screen.findByRole('button', { name: 'Revise Plan to Fit Budget' })
    await act(async () => { button.click() })
    expect(screen.getByRole('heading', { name: 'Loading your revision result' })).toBeVisible()
    expect(screen.queryByText('Budget Review Completed')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Budget revision results')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Revise Plan to Fit Budget' })).not.toBeInTheDocument()
    await act(async () => { finishDetails(final) })
    expect(screen.getByRole('heading', { name: 'Budget Review Completed' })).toBeVisible()
    expect(screen.getByLabelText('Revision outcome explanation')).toHaveTextContent('No further compatible savings')
    expect(reviseTrip).toHaveBeenCalledTimes(1)
    expect(getTripStatus).toHaveBeenCalledTimes(2)
    expect(getTrip).toHaveBeenCalledTimes(2)
  })

  it('keeps a final-details error visible instead of reusing a stale completed revision', async () => {
    vi.useFakeTimers()
    try {
      const previous = outcomeTrip('improved')
      previous.budget_revision_summary!.can_retry = true
      vi.mocked(getTripStatus).mockResolvedValue({ ...completedStatus, status: 'revision_required' })
      vi.mocked(getTrip).mockResolvedValueOnce(previous).mockRejectedValueOnce(new ApiError('Final revision details unavailable.', 503))
        .mockResolvedValue(outcomeTrip('no_supported_alternative'))
      vi.mocked(reviseTrip).mockResolvedValue({ trip_id: 'trip-123', task_id: 'trip-123', status: 'queued', message: 'Revision accepted.' })
      render(<TripWorkspace tripId="trip-123" initialStatus="queued" onStartOver={vi.fn()} />)
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      await act(async () => { screen.getByRole('button', { name: 'Revise Plan to Fit Budget' }).click() })
      expect(screen.getByRole('alert')).toHaveTextContent('Final revision details unavailable.')
      expect(screen.queryByText('Budget Review Completed')).not.toBeInTheDocument()
      expect(screen.queryByLabelText('Budget revision results')).not.toBeInTheDocument()
      await act(async () => { await vi.advanceTimersByTimeAsync(7000) })
      expect(screen.getByRole('heading', { name: 'Budget Review Completed' })).toBeVisible()
      expect(screen.queryByText('Final revision details unavailable.')).not.toBeInTheDocument()
      expect(reviseTrip).toHaveBeenCalledTimes(1)
    } finally { vi.useRealTimers() }
  })

})
