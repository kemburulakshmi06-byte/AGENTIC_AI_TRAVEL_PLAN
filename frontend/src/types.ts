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
  cost_preferences?: TripCostPreferences
  user_message?: string
}

export interface TripCostPreferences {
  accommodation_nightly_rate_per_room?: string
  room_count?: number
  transport_mode?: 'driving' | 'walking' | 'cycling' | 'local_transit' | 'flight' | 'train' | 'bus'
  intercity_round_trip_fare_per_traveler?: string
  local_transport_allowance_per_day?: string
  food_allowance_per_traveler_per_day?: string
  activities_allowance_per_traveler_per_trip?: string
  contingency_percentage?: string
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
  review_status: 'APPROVED' | 'REVISION_REQUIRED' | 'INSUFFICIENT_DATA'
  issues?: OptimizationIssue[]
  suggestions?: OptimizationSuggestion[]
  budget_status?: string
  estimated_savings?: string | null
  currency?: string | null
  warnings?: string[]
  source_references?: SourceReference[]
  qualitative_summary?: string | null
  [key: string]: unknown
}

export interface TripDetails {
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
  }
  total_estimated_cost: string | null
  currency: string
  created_at: string
  updated_at: string
  itinerary: Record<string, unknown> | null
  budget: Record<string, unknown> | null
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
}

export interface TripListResponse {
  items: TripSummary[]
  total: number
  limit: number
  offset: number
}
