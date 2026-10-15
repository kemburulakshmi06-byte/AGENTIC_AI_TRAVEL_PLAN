export type TripStatus =
  | 'planned'
  | 'queued'
  | 'running'
  | 'completed'
  | 'failed'
  | 'revision_required'
  | 'insufficient_data'

export interface TripPlanRequest {
  origin: string
  destination: string
  start_date: string
  end_date: string
  travelers_count: number
  total_budget: string
  currency: string
  travel_style?: string
  interests: string[]
  constraints: Record<string, unknown>
  cost_preferences?: TripCostPreferences | null
  user_message?: string
}

export interface TripCostPreferences {
  accommodation_nightly_rate_per_room?: string | null
  room_count?: number | null
  transport_mode?: 'driving' | 'walking' | 'cycling' | 'local_transit' | 'flight' | 'train' | 'bus' | null
  intercity_round_trip_fare_per_traveler?: string | null
  local_transport_allowance_per_day?: string | null
  food_allowance_per_traveler_per_day?: string | null
  activities_allowance_per_traveler_per_trip?: string | null
  contingency_percentage?: string | null
}

export interface TripCreatedResponse {
  trip_id: string
  task_id: string
  status: TripStatus
  message: string
}

export interface TripStatusResponse {
  trip_id: string
  task_id: string
  status: TripStatus
  current_stage: string | null
  progress_percentage: number
  message: string | null
  error_message: string | null
  created_at: string
  updated_at: string
}

export interface PersistedActivity {
  title: string
  description: string | null
  location_name: string | null
  start_time: string | null
  end_time: string | null
  estimated_cost: string | null
  category: string | null
  metadata: Record<string, unknown>
}

export interface PersistedDay {
  day_number: number
  itinerary_date: string
  summary: string | null
  estimated_daily_cost: string | null
  activities: PersistedActivity[]
}

export interface SourceReference {
  title: string
  url: string
  provider?: string
  topic?: 'attractions' | 'history' | 'food' | 'travel_tips' | 'seasonal' | 'general'
  content_is_untrusted?: true
}

export type BudgetStatus = 'INCOMPLETE_ESTIMATE' | 'WITHIN_BUDGET' | 'OVER_BUDGET' | 'INVALID_INPUT'
export type BudgetCategory = 'accommodation' | 'local_transport' | 'intercity_transport' | 'food' | 'activities' | 'contingency' | 'other'
export type EstimateConfidence = 'high' | 'medium' | 'low' | 'unassessed' | 'unknown'
export type ReviewStatus = 'APPROVED' | 'REVISION_REQUIRED' | 'INSUFFICIENT_DATA'

// Optional metadata supports older persisted responses. Decimal values remain strings.
export interface PriceSource extends Record<string, unknown> {
  name: string
  url?: string | null
  observed_on?: string
  currency?: string
  published_on?: string | null
}

export interface CurrencyConversion extends Record<string, unknown> {
  original_currency?: string
  original_amount?: string
  target_currency?: string
  rate?: string
  rate_date?: string
  source?: string
  source_url?: string
}

export interface PassengerPricingBasis {
  kind: 'unknown' | 'per_adult' | 'party_total'
  priced_passengers?: number | null
  evidence?: string | null
}

export interface FlightPricingScope {
  origin_iata: string
  destination_iata: string
  origin_location: string
  destination_location: string
  departure_date: string
  return_date: string
  currency: string
  travelers: number
}

export interface CachedFlightReference extends Record<string, unknown> {
  provider: 'travelpayouts'
  classification: 'cached_reference'
  live_availability_checked?: false
  origin_iata: string
  destination_iata: string
  departure_date?: string | null
  return_date?: string | null
  currency?: string | null
  price: string
  passenger_basis?: PassengerPricingBasis
  retrieved_at: string
  found_at?: string | null
  expires_at?: string | null
  requested_scope: FlightPricingScope
  applicability?: string
}

// Additive AI metadata emitted by the backend; older saved plans omit it.
export interface AICostScope {
  origin: string
  destination: string
  start_date: string
  end_date: string
  travelers_count: number
  room_count: number
  travel_style: string
  transport_mode: NonNullable<TripCostPreferences['transport_mode']>
  currency: string
}

export interface AIUnitRate {
  category: Exclude<BudgetCategory, 'contingency' | 'other'>
  unit: 'traveler_round_trip' | 'room_night' | 'traveler_day' | 'party_day' | 'traveler_trip'
  currency: string
  typical: string
  minimum?: string | null
  maximum?: string | null
}

export interface AICostProvenance {
  method: 'gemini_planning_assumption'
  confidence: 'low'
  model: string
  generated_at: string
  scope: AICostScope
  generation_scope?: AICostScope | null
  unit_rate: AIUnitRate
  quantity: number
  live_availability_checked: false
}

export interface CostEvidence extends Record<string, unknown> {
  pricing_basis?: string | null
  assumptions?: string[]
  source_references?: SourceReference[]
  evidence_kind?: 'planning_estimate' | 'verified_price' | 'unknown'
  minimum_amount?: string | null
  maximum_amount?: string | null
  confidence?: EstimateConfidence
  estimated_on?: string
  price_sources?: PriceSource[]
  currency_conversion?: CurrencyConversion | null
  flight_reference?: CachedFlightReference | null
  ai_provenance?: AICostProvenance | null
}

export interface BudgetLineItem extends CostEvidence {
  category?: BudgetCategory
  description?: string
  amount?: string | null
  currency?: string
  classification?: 'estimated' | 'verified' | 'unknown'
  source?: 'verified_provider' | 'user_assumption' | 'configured_assumption' | 'reference_data' | 'unknown'
  optional?: boolean
}

export interface BudgetCategoryTotal extends Record<string, unknown> {
  category: BudgetCategory
  known_subtotal: string
  currency?: string
  complete?: boolean
  included?: boolean
  line_item_count?: number
}

export interface BudgetReductionSuggestion extends Record<string, unknown> {
  category: BudgetCategory
  recommendation: string
  estimated_savings?: string | null
  currency?: string | null
  pricing_basis?: string | null
  assumptions?: string[]
  source_references?: SourceReference[]
}

export interface BudgetBreakdown extends Record<string, unknown> {
  total_budget?: string | null
  currency?: string | null
  known_cost_subtotal?: string | null
  estimated_total?: string | null
  remaining_budget?: string | null
  over_budget_amount?: string | null
  status?: BudgetStatus
  minimum_estimated_total?: string | null
  maximum_estimated_total?: string | null
  estimate_confidence?: EstimateConfidence
  booking_readiness?: 'unverified'
  pricing_status?: 'COMPLETE_ESTIMATE' | 'LIVE_PRICE_VERIFIED' | 'INCOMPLETE'
  missing_price_actions?: string[]
  missing_price_inputs?: string[]
  flight_references?: CachedFlightReference[]
  line_items?: BudgetLineItem[]
  category_totals?: BudgetCategoryTotal[]
  unpriced_components?: string[]
  warnings?: string[]
  reduction_suggestions?: BudgetReductionSuggestion[]
  qualitative_summary?: string | null
  qualitative_tradeoffs?: string[]
}

export interface OptimizationIssue {
  code?: string
  severity?: string
  description: string
  evidence_basis?: string
  recommended_action?: string
  blocks_approval?: boolean
  date?: string | null
  day_number?: number | null
  activity_title?: string | null
  place_id?: string | null
  [key: string]: unknown
}

export interface OptimizationSuggestion {
  category: string
  recommendation: string
  date?: string | null
  day_number?: number | null
  estimated_savings?: string | null
  currency?: string | null
  savings_basis?: string | null
  source_references?: SourceReference[]
  [key: string]: unknown
}

export interface OptimizationReview {
  approved: boolean
  review_status: ReviewStatus
  issues?: OptimizationIssue[]
  suggestions?: OptimizationSuggestion[]
  budget_status?: BudgetStatus
  estimated_savings?: string | null
  currency?: string | null
  warnings?: string[]
  source_references?: SourceReference[]
  qualitative_summary?: string | null
  [key: string]: unknown
}

export interface TripRevisionRecord {
  attempt: number
  original_total: string
  revised_total: string | null
  savings: string | null
  remaining_over_budget: string | null
  currency: string
  outcome: string
  changes: string[]
  explanation: string
  outcome_message?: string | null
  original_budget?: string | null
  cumulative_savings?: string | null
  alternatives_exhausted?: boolean
}

export interface TripRevisionSummary {
  attempts: number
  max_attempts: number
  can_retry: boolean
  requires_economy_permission?: boolean
  economy_accommodation_available?: boolean | null
  history: TripRevisionRecord[]
}

export interface TripDetails {
  budget_revision_summary?: TripRevisionSummary | null
  id: string
  title: string
  status: TripStatus
  request: {
    origin: string
    destination: string
    start_date: string
    end_date: string
    travelers_count: number
    total_budget: string
    currency: string
    travel_style?: string | null
    interests?: string[]
    constraints?: Record<string, unknown>
    cost_preferences?: TripCostPreferences | null
  }
  total_estimated_cost: string | null
  currency: string
  created_at: string
  updated_at: string
  itinerary: Record<string, unknown> | null
  budget: BudgetBreakdown | null
  optimization_review: OptimizationReview | null
  accommodation_transport?: Record<string, unknown> | null
  normalized_requirements?: Record<string, unknown> | null
  destination_research?: Record<string, unknown> | null
  days: PersistedDay[]
  warnings: string[]
  errors: string[]
}

export interface TripSummary {
  id: string
  title: string
  status: TripStatus
  origin: string
  destination: string
  start_date: string
  end_date: string
  travelers_count: number
  total_budget: string
  currency: string
  total_estimated_cost: string | null
  created_at?: string
  updated_at?: string
}

export interface TripListResponse {
  items: TripSummary[]
  total: number
  limit: number
  offset: number
}
