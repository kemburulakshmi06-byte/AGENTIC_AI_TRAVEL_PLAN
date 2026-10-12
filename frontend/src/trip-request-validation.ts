import type { TripCostPreferences, TripPlanRequest } from './types'

export const COST_INPUT_LABELS: Record<string, string> = {
  accommodation_nightly_rate_per_room: 'Hotel cost per room per night',
  room_count: 'Number of rooms',
  intercity_round_trip_fare_per_traveler: 'Round-trip transport per traveler',
  local_transport_allowance_per_day: 'Local transport per day',
  food_allowance_per_traveler_per_day: 'Food per traveler per day',
  activities_allowance_per_traveler_per_trip: 'Activities per traveler per trip',
  contingency_percentage: 'Contingency percentage',
}

export function missingCostPreferenceInputs(
  preferences: TripCostPreferences | undefined,
  overnight: boolean,
): string[] {
  return [
    ...(overnight ? ['accommodation_nightly_rate_per_room'] : []),
    'intercity_round_trip_fare_per_traveler',
    'local_transport_allowance_per_day',
    'food_allowance_per_traveler_per_day',
    'activities_allowance_per_traveler_per_trip',
  ].filter((field) => preferences?.[field as keyof TripCostPreferences] == null)
}

interface CostConstraintValues {
  stepFreeAccess: boolean
}

export interface CostPreferenceValues {
  accommodationNightlyBudget: string
  roomCount: string
  transportMode: TripCostPreferences['transport_mode'] | ''
  roundTripTransportAllowance: string
  dailyLocalTransportBudget: string
  dailyFoodBudget: string
  activityBudget: string
  contingencyPercentage: string
}

export function buildConstraints(
  values: CostConstraintValues,
): Record<string, unknown> {
  const constraints: Record<string, unknown> = {}
  if (values.stepFreeAccess) {
    constraints.accessibility = 'step-free access required'
  }

  return constraints
}

export function buildCostPreferences(
  values: CostPreferenceValues,
): TripCostPreferences | undefined {
  const preferences: TripCostPreferences = {}
  if (values.accommodationNightlyBudget.trim()) {
    preferences.accommodation_nightly_rate_per_room =
      values.accommodationNightlyBudget
  }
  if (values.roomCount.trim()) {
    preferences.room_count = Number(values.roomCount)
  }
  if (values.transportMode) preferences.transport_mode = values.transportMode
  if (values.roundTripTransportAllowance.trim()) {
    preferences.intercity_round_trip_fare_per_traveler =
      values.roundTripTransportAllowance
  }
  if (values.dailyLocalTransportBudget.trim()) {
    preferences.local_transport_allowance_per_day =
      values.dailyLocalTransportBudget
  }
  if (values.dailyFoodBudget.trim()) {
    preferences.food_allowance_per_traveler_per_day = values.dailyFoodBudget
  }
  if (values.activityBudget.trim()) {
    preferences.activities_allowance_per_traveler_per_trip =
      values.activityBudget
  }
  if (values.contingencyPercentage.trim()) {
    preferences.contingency_percentage = values.contingencyPercentage
  }
  return Object.keys(preferences).length ? preferences : undefined
}

export function validateTripRequest(
  values: TripPlanRequest,
  costValues?: CostPreferenceValues,
): string | undefined {
  if (!values.origin.trim() || !values.destination.trim()) {
    return 'Add both an origin and a destination.'
  }
  if (!values.start_date || !values.end_date) {
    return 'Choose your travel dates.'
  }
  if (values.end_date < values.start_date) {
    return 'Your return date must be on or after your start date.'
  }
  if (!Number.isInteger(values.travelers_count) || values.travelers_count < 1) {
    return 'Travelers must be at least one person.'
  }
  if (
    !Number.isFinite(Number(values.total_budget)) ||
    Number(values.total_budget) <= 0
  ) {
    return 'Enter a budget greater than zero.'
  }
  if (!/^[A-Z]{3}$/.test(values.currency)) {
    return 'Choose a valid three-letter currency.'
  }
  if (costValues) {
    const numericValues = [
      costValues.accommodationNightlyBudget,
      costValues.roundTripTransportAllowance,
      costValues.dailyLocalTransportBudget,
      costValues.dailyFoodBudget,
      costValues.activityBudget,
      costValues.contingencyPercentage,
    ]
    if (
      numericValues.some(
        (raw) => raw.trim() && (!Number.isFinite(Number(raw)) || Number(raw) < 0),
      )
    ) {
      return 'Cost preferences must be non-negative numbers.'
    }
    if (
      costValues.roomCount.trim() &&
      (!Number.isInteger(Number(costValues.roomCount)) ||
        Number(costValues.roomCount) < 1 ||
        Number(costValues.roomCount) > 50)
    ) {
      return 'Room count must be a whole number from 1 to 50.'
    }
    if (
      costValues.contingencyPercentage.trim() &&
      Number(costValues.contingencyPercentage) > 100
    ) {
      return 'Contingency must be between 0 and 100 percent.'
    }
  }
}
