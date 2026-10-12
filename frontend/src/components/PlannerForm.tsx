import { useState, type FormEvent } from 'react'
import { planTrip } from '../api'
import type { TripCreatedResponse, TripPlanRequest } from '../types'
import {
  buildConstraints,
  buildCostPreferences,
  validateTripRequest,
  COST_INPUT_LABELS,
  missingCostPreferenceInputs,
} from '../trip-request-validation'

const INTERESTS = [
  'Culture',
  'Food',
  'Nature',
  'Art',
  'Adventure',
  'History',
  'Shopping',
  'Relaxation',
]

interface PlannerFormProps {
  onTripCreated: (trip: TripCreatedResponse) => void
}

function localDate(daysFromNow: number): string {
  const date = new Date()
  date.setDate(date.getDate() + daysFromNow)
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${date.getFullYear()}-${month}-${day}`
}

export function PlannerForm({ onTripCreated }: PlannerFormProps) {
  const [origin, setOrigin] = useState('')
  const [destination, setDestination] = useState('')
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')
  const [travelers, setTravelers] = useState('')
  const [budget, setBudget] = useState('')
  const [currency, setCurrency] = useState('')
  const [style, setStyle] = useState('balanced')
  const [interests, setInterests] = useState<string[]>([])
  const [preferences, setPreferences] = useState('')
  const [stepFreeAccess, setStepFreeAccess] = useState(false)
  const [accommodationNightlyBudget, setAccommodationNightlyBudget] = useState('')
  const [roomCount, setRoomCount] = useState('')
  const [transportMode, setTransportMode] = useState<
    'driving' | 'walking' | 'cycling' | 'local_transit' | 'flight' | 'train' | 'bus' | ''
  >('')
  const [roundTripTransportAllowance, setRoundTripTransportAllowance] = useState('')
  const [dailyLocalTransportBudget, setDailyLocalTransportBudget] = useState('')
  const [dailyFoodBudget, setDailyFoodBudget] = useState('')
  const [activityBudget, setActivityBudget] = useState('')
  const [contingencyPercentage, setContingencyPercentage] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const missingCosts = missingCostPreferenceInputs(buildCostPreferences({
    accommodationNightlyBudget, roomCount, transportMode,
    roundTripTransportAllowance, dailyLocalTransportBudget,
    dailyFoodBudget, activityBudget, contingencyPercentage,
  }), startDate !== endDate)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (busy) return
    const payload: TripPlanRequest = {
      origin: origin.trim(),
      destination: destination.trim(),
      start_date: startDate,
      end_date: endDate,
      travelers_count: Number(travelers),
      total_budget: budget,
      currency,
      travel_style: style,
      interests,
      constraints: buildConstraints({ stepFreeAccess }),
      user_message: preferences.trim() || undefined,
    }
    const costPreferences = buildCostPreferences({
      accommodationNightlyBudget,
      roomCount,
      transportMode,
      roundTripTransportAllowance,
      dailyLocalTransportBudget,
      dailyFoodBudget,
      activityBudget,
      contingencyPercentage,
    })
    if (costPreferences) payload.cost_preferences = costPreferences
    const validationError = validateTripRequest(payload, {
      accommodationNightlyBudget,
      roomCount,
      transportMode,
      roundTripTransportAllowance,
      dailyLocalTransportBudget,
      dailyFoodBudget,
      activityBudget,
      contingencyPercentage,
    })
    if (validationError) {
      setError(validationError)
      return
    }
    setError('')
    setBusy(true)
    try {
      onTripCreated(await planTrip(payload))
    } catch (submissionError) {
      setError(
        submissionError instanceof Error
          ? submissionError.message
          : 'We could not start your trip. Please try again.',
      )
    } finally {
      setBusy(false)
    }
  }

  function toggleInterest(interest: string) {
    setInterests((selected) =>
      selected.includes(interest)
        ? selected.filter((item) => item !== interest)
        : [...selected, interest],
    )
  }

  return (
    <form className="planner-form" onSubmit={handleSubmit} noValidate>
      <div className="form-heading">
        <span className="eyebrow">YOUR NEXT CHAPTER</span>
        <h2>Plan a trip that feels like you.</h2>
        <p>Share a few details. Your AI travel team will take it from here.</p>
      </div>

      <div className="form-grid">
        <label className="field field-wide">
          <span>Where are you starting?</span>
          <input
            autoComplete="address-level2"
            name="origin"
            placeholder="Origin city and country"
            value={origin}
            onChange={(event) => setOrigin(event.target.value)}
            required
          />
        </label>
        <label className="field field-wide">
          <span>Where do you want to go?</span>
          <input
            name="destination"
            placeholder="City, region or country; use > between cities"
            value={destination}
            onChange={(event) => setDestination(event.target.value)}
            required
          />
        </label>
        <label className="field">
          <span>Departure</span>
          <input
            aria-label="Departure date"
            type="date"
            min={localDate(0)}
            value={startDate}
            onChange={(event) => setStartDate(event.target.value)}
            required
          />
        </label>
        <label className="field">
          <span>Return</span>
          <input
            aria-label="Return date"
            type="date"
            min={startDate || localDate(0)}
            value={endDate}
            onChange={(event) => setEndDate(event.target.value)}
            required
          />
        </label>
        <label className="field">
          <span>Travelers</span>
          <input
            aria-label="Number of travelers"
            type="number"
            min="1"
            max="20"
            value={travelers}
            onChange={(event) => setTravelers(event.target.value)}
            required
          />
        </label>
        <label className="field">
          <span>Total trip budget</span>
          <div className="money-input">
            <input
              aria-label="Total trip budget"
              type="number"
              min="0.01"
              step="0.01"
              placeholder="Enter budget"
              value={budget}
              onChange={(event) => setBudget(event.target.value)}
              required
            />
            <select
              aria-label="Currency"
              value={currency}
              onChange={(event) => setCurrency(event.target.value)}
            >
              <option value="">Currency</option>
              <option>USD</option>
              <option>EUR</option>
              <option>GBP</option>
              <option>INR</option>
              <option>CAD</option>
              <option>AUD</option>
              <option>JPY</option>
              <option>AED</option>
            </select>
          </div>
        </label>
        <label className="field">
          <span>Travel style</span>
          <select
            value={style}
            onChange={(event) => setStyle(event.target.value)}
          >
            <option value="balanced">A little of everything</option>
            <option value="budget">Thoughtful on a budget</option>
            <option value="comfort">Comfort first</option>
            <option value="luxury">A special occasion</option>
            <option value="adventure">Out and exploring</option>
          </select>
        </label>
      </div>

      <fieldset className="interest-fieldset">
        <legend>What are you into?</legend>
        <div className="interest-list">
          {INTERESTS.map((interest) => (
            <button
              type="button"
              className={`interest-chip ${interests.includes(interest) ? 'selected' : ''}`}
              aria-pressed={interests.includes(interest)}
              key={interest}
              onClick={() => toggleInterest(interest)}
            >
              {interest}
            </button>
          ))}
        </div>
      </fieldset>

      <label className="accessibility-option">
        <input
          type="checkbox"
          checked={stepFreeAccess}
          onChange={(event) => setStepFreeAccess(event.target.checked)}
        />
        <span>Step-free access is important</span>
      </label>

      <details className="cost-assumptions">
        <summary>Trip Cost Preferences <small>Optional · no live hotel or fare prices</small></summary>
        <p>
          These values are planning assumptions, not live prices or quotes.
          Valid provider prices and supported dated references take priority;
          these allowances fill remaining gaps. Leave unknown values blank.
          Costs without supported pricing or allowances keep the budget incomplete.
          If rooms are blank, the planner assumes up to two travelers per room.
        </p>
        <div className="form-grid">
          <label className="field">
            <span>Accommodation nightly budget / room ({currency})</span>
            <input
              aria-label="Accommodation nightly budget"
              type="number"
              min="0"
              step="0.01"
              placeholder="Optional estimate"
              value={accommodationNightlyBudget}
              onChange={(event) => setAccommodationNightlyBudget(event.target.value)}
            />
          </label>
          <label className="field">
            <span>Number of rooms</span>
            <input
              aria-label="Number of rooms"
              type="number"
              min="1"
              max="50"
              step="1"
              placeholder="Optional"
              value={roomCount}
              onChange={(event) => setRoomCount(event.target.value)}
            />
          </label>
          <label className="field">
            <span>Preferred transport mode</span>
            <select
              aria-label="Transport mode"
              value={transportMode}
              onChange={(event) => setTransportMode(event.target.value as typeof transportMode)}
            >
              <option value="">No preference</option>
              <option value="flight">Flight</option>
              <option value="train">Train</option>
              <option value="bus">Bus</option>
              <option value="driving">Driving</option>
              <option value="local_transit">Local transit</option>
              <option value="walking">Walking</option>
              <option value="cycling">Cycling</option>
            </select>
          </label>
          <label className="field">
            <span>Round-trip transport / traveler ({currency})</span>
            <input
              aria-label="Round-trip transport allowance"
              type="number"
              min="0"
              step="0.01"
              placeholder="Optional estimate"
              value={roundTripTransportAllowance}
              onChange={(event) => setRoundTripTransportAllowance(event.target.value)}
            />
          </label>
          <label className="field">
            <span>Local transport / day ({currency})</span>
            <input
              aria-label="Daily local transport budget"
              type="number"
              min="0"
              step="0.01"
              placeholder="Optional estimate"
              value={dailyLocalTransportBudget}
              onChange={(event) => setDailyLocalTransportBudget(event.target.value)}
            />
          </label>
          <label className="field">
            <span>Food / traveler / day ({currency})</span>
            <input
              aria-label="Daily food budget"
              type="number"
              min="0"
              step="0.01"
              placeholder="Optional estimate"
              value={dailyFoodBudget}
              onChange={(event) => setDailyFoodBudget(event.target.value)}
            />
          </label>
          <label className="field">
            <span>Activities / traveler / trip ({currency})</span>
            <input
              aria-label="Activity budget"
              type="number"
              min="0"
              step="0.01"
              placeholder="Optional estimate"
              value={activityBudget}
              onChange={(event) => setActivityBudget(event.target.value)}
            />
          </label>
          <label className="field">
            <span>Contingency (%)</span>
            <input
              aria-label="Contingency percentage"
              type="number"
              min="0"
              max="100"
              step="0.01"
              placeholder="Optional"
              value={contingencyPercentage}
              onChange={(event) => setContingencyPercentage(event.target.value)}
            />
          </label>
        </div>
      </details>
      <p className="form-note" role="status">
        {missingCosts.length
          ? `Automatic pricing and supported dated estimates will be attempted. Optional fallback allowances: ${missingCosts.map((field) => COST_INPUT_LABELS[field]).join(', ')}. Unsupported costs remain unknown.`
          : 'All required planning estimates supplied. Affordability and itinerary feasibility will still be reviewed; prices are not live quotes.'}
      </p>

      <label className="field preferences-field">
        <span>Anything else we should know? <small>Optional</small></span>
        <textarea
          maxLength={1000}
          placeholder="Dietary needs, a slower pace, traveling with kids..."
          rows={3}
          value={preferences}
          onChange={(event) => setPreferences(event.target.value)}
        />
      </label>

      {error && (
        <div className="form-error" role="alert">
          <span aria-hidden="true">!</span> {error}
        </div>
      )}
      <button className="button button-primary submit-button" disabled={busy}>
        {busy ? (
          <>
            <span className="button-spinner" /> Getting things ready...
          </>
        ) : (
          <>
            Build my trip <span aria-hidden="true">↗</span>
          </>
        )}
      </button>
      <p className="form-note">Your budget is a hard limit, not a suggestion.</p>
    </form>
  )
}
