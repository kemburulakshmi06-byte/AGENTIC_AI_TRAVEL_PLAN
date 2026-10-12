import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { planTrip } from '../api'
import {
  buildCostPreferences,
  validateTripRequest,
  missingCostPreferenceInputs,
} from '../trip-request-validation'
import { PlannerForm } from './PlannerForm'
import type { TripPlanRequest } from '../types'

vi.mock('../api', () => ({
  planTrip: vi.fn(),
}))

describe('missing cost input guidance', () => {
  it('distinguishes blank prices from explicit zero estimates', () => {
    expect(missingCostPreferenceInputs(undefined, true)).toHaveLength(5)
    expect(missingCostPreferenceInputs({
      accommodation_nightly_rate_per_room: '0.00',
      intercity_round_trip_fare_per_traveler: '0.00',
      local_transport_allowance_per_day: '0.00',
      food_allowance_per_traveler_per_day: '0.00',
      activities_allowance_per_traveler_per_trip: '0.00',
    }, true)).toEqual([])
  })

  it('does not request a hotel price for a zero-night trip', () => {
    expect(missingCostPreferenceInputs(undefined, false)).not.toContain('accommodation_nightly_rate_per_room')
  })
})

const validRequest: TripPlanRequest = {
  origin: 'Seattle',
  destination: 'Kyoto',
  start_date: '2030-05-01',
  end_date: '2030-05-06',
  travelers_count: 2,
  total_budget: '3500',
  currency: 'USD',
  interests: ['Culture'],
  constraints: {},
}

describe('trip request validation', () => {
  it('rejects missing locations and invalid travel dates', () => {
    expect(validateTripRequest({ ...validRequest, origin: ' ' })).toContain(
      'origin',
    )
    expect(
      validateTripRequest({
        ...validRequest,
        end_date: '2030-04-30',
      }),
    ).toContain('return date')
  })

  it('rejects non-positive budgets and traveler counts', () => {
    expect(
      validateTripRequest({ ...validRequest, total_budget: '0' }),
    ).toContain('budget')
    expect(
      validateTripRequest({ ...validRequest, travelers_count: 0 }),
    ).toContain('Travelers')
  })

  it('accepts an exact-schema valid request', () => {
    expect(validateTripRequest(validRequest)).toBeUndefined()
  })

  it('maps optional trip cost preferences without inventing empty values', () => {
    expect(
      buildCostPreferences({
        accommodationNightlyBudget: '1250.50',
        roomCount: '2',
        transportMode: 'train',
        roundTripTransportAllowance: '5000',
        dailyLocalTransportBudget: '300',
        dailyFoodBudget: '750',
        activityBudget: '1200',
        contingencyPercentage: '10',
      }),
    ).toEqual({
      accommodation_nightly_rate_per_room: '1250.50',
      room_count: 2,
      transport_mode: 'train',
      intercity_round_trip_fare_per_traveler: '5000',
      local_transport_allowance_per_day: '300',
      food_allowance_per_traveler_per_day: '750',
      activities_allowance_per_traveler_per_trip: '1200',
      contingency_percentage: '10',
    })
    expect(
      buildCostPreferences({
        accommodationNightlyBudget: '',
        roomCount: '',
        transportMode: '',
        roundTripTransportAllowance: '',
        dailyLocalTransportBudget: '',
        dailyFoodBudget: '',
        activityBudget: '',
        contingencyPercentage: '',
      }),
    ).toBeUndefined()
  })

  it('rejects invalid optional cost preferences', () => {
    const preferences = {
      accommodationNightlyBudget: '',
      roomCount: '1.5',
      transportMode: '',
      roundTripTransportAllowance: '-1',
      dailyLocalTransportBudget: '',
      dailyFoodBudget: '',
      activityBudget: '',
      contingencyPercentage: '101',
    } as const
    expect(validateTripRequest(validRequest, preferences)).toContain(
      'non-negative',
    )
    expect(
      validateTripRequest(validRequest, { ...preferences, roundTripTransportAllowance: '' }),
    ).toContain('Room count')
  })
})

describe('PlannerForm', () => {
  it('requires trip dates, travelers and currency from the user instead of supplying defaults', () => {
    render(<PlannerForm onTripCreated={vi.fn()} />)
    expect(screen.getByLabelText('Departure date')).toHaveValue('')
    expect(screen.getByLabelText('Return date')).toHaveValue('')
    expect(screen.getByLabelText('Number of travelers')).toHaveValue(null)
    expect(screen.getByLabelText('Currency')).toHaveValue('')
    expect(screen.getByLabelText(/where do you want to go/i)).toHaveValue('')
  })
  beforeEach(() => {
    vi.mocked(planTrip).mockResolvedValue({
      trip_id: 'trip-123',
      task_id: 'task-123',
      status: 'queued',
      message: 'Accepted',
    })
  })

  it('validates before sending and forwards the Phase 8 request shape', async () => {
    const onTripCreated = vi.fn()
    render(<PlannerForm onTripCreated={onTripCreated} />)

    fireEvent.click(screen.getByRole('button', { name: /build my trip/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'origin and a destination',
    )
    expect(planTrip).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText(/where are you starting/i), {
      target: { value: ' Seattle ' },
    })
    fireEvent.change(screen.getByLabelText(/where do you want to go/i), {
      target: { value: 'Kyoto' },
    })
    fireEvent.change(screen.getByLabelText('Total trip budget'), {
      target: { value: '3500.50' },
    })
    fireEvent.change(screen.getByLabelText('Departure date'), {
      target: { value: '2030-05-01' },
    })
    fireEvent.change(screen.getByLabelText('Return date'), {
      target: { value: '2030-05-06' },
    })
    fireEvent.change(screen.getByLabelText('Number of travelers'), {
      target: { value: '3' },
    })
    fireEvent.change(screen.getByLabelText('Currency'), {
      target: { value: 'INR' },
    })
    fireEvent.change(screen.getByLabelText('Travel style'), {
      target: { value: 'comfort' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Culture' }))
    fireEvent.click(
      screen.getByRole('checkbox', { name: /step-free access/i }),
    )
    fireEvent.change(screen.getByLabelText('Accommodation nightly budget'), {
      target: { value: '1250.50' },
    })
    fireEvent.change(screen.getByLabelText('Number of rooms'), {
      target: { value: '2' },
    })
    fireEvent.change(screen.getByLabelText('Transport mode'), {
      target: { value: 'train' },
    })
    fireEvent.change(screen.getByLabelText('Round-trip transport allowance'), {
      target: { value: '5000' },
    })
    fireEvent.change(screen.getByLabelText('Daily local transport budget'), {
      target: { value: '500' },
    })
    fireEvent.change(screen.getByLabelText('Daily food budget'), {
      target: { value: '300' },
    })
    fireEvent.change(screen.getByLabelText('Activity budget'), {
      target: { value: '1000' },
    })
    fireEvent.change(screen.getByLabelText('Contingency percentage'), {
      target: { value: '10' },
    })
    fireEvent.change(
      screen.getByPlaceholderText(/dietary needs/i),
      { target: { value: 'Vegetarian, relaxed pace' } },
    )

    fireEvent.click(screen.getByRole('button', { name: /build my trip/i }))

    await waitFor(() =>
      expect(onTripCreated).toHaveBeenCalledWith(
        expect.objectContaining({ trip_id: 'trip-123' }),
      ),
    )
    expect(planTrip).toHaveBeenCalledWith(
      expect.objectContaining({
        origin: 'Seattle',
        destination: 'Kyoto',
        start_date: '2030-05-01',
        end_date: '2030-05-06',
        travelers_count: 3,
        total_budget: '3500.50',
        currency: 'INR',
        travel_style: 'comfort',
        interests: ['Culture'],
        constraints: {
          accessibility: 'step-free access required',
        },
        cost_preferences: {
          accommodation_nightly_rate_per_room: '1250.50',
          room_count: 2,
          transport_mode: 'train',
          intercity_round_trip_fare_per_traveler: '5000',
          local_transport_allowance_per_day: '500',
          food_allowance_per_traveler_per_day: '300',
          activities_allowance_per_traveler_per_trip: '1000',
          contingency_percentage: '10',
        },
        user_message: 'Vegetarian, relaxed pace',
      }),
    )
  })

  it('shows safe API error text without internal traces', async () => {
    vi.mocked(planTrip).mockRejectedValue(
      new Error('The travel service is unavailable.'),
    )
    render(<PlannerForm onTripCreated={vi.fn()} />)
    fireEvent.change(screen.getByLabelText(/where are you starting/i), {
      target: { value: 'Seattle' },
    })
    fireEvent.change(screen.getByLabelText(/where do you want to go/i), {
      target: { value: 'Kyoto' },
    })
    fireEvent.change(screen.getByLabelText('Total trip budget'), {
      target: { value: '3500' },
    })
    fireEvent.change(screen.getByLabelText('Departure date'), { target: { value: '2030-05-01' } })
    fireEvent.change(screen.getByLabelText('Return date'), { target: { value: '2030-05-06' } })
    fireEvent.change(screen.getByLabelText('Number of travelers'), { target: { value: '2' } })
    fireEvent.change(screen.getByLabelText('Currency'), { target: { value: 'USD' } })
    fireEvent.click(screen.getByRole('button', { name: /build my trip/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'travel service is unavailable',
    )
  })
})
