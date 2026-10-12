import { useEffect, useState } from 'react'
import { ApiError, getTrip, getTripStatus } from '../api'
import { COST_INPUT_LABELS } from '../trip-request-validation'
import type {
  OptimizationReview,
  PersistedActivity,
  TripDetails,
  TripStatus,
  TripStatusResponse,
} from '../types'

const TERMINAL_STATUSES = new Set<TripStatus>([
  'planned',
  'completed',
  'failed',
  'revision_required',
  'insufficient_data',
])

const STAGES = [
  ['queued', 'Request received'],
  ['requirements', 'Understanding your trip'],
  ['destination_research', 'Exploring your destination'],
  ['accommodation_transport', 'Finding your way around'],
  ['budget', 'Balancing your budget'],
  ['itinerary', 'Building your itinerary'],
  ['optimization_review', 'Checking the details'],
]

interface TripWorkspaceProps {
  tripId: string
  initialStatus: TripStatus
  onStartOver: () => void
}

export function TripWorkspace({
  tripId,
  initialStatus,
  onStartOver,
}: TripWorkspaceProps) {
  const [storedStatus, setStatus] = useState<TripStatusResponse | null>(null)
  const [storedTrip, setTrip] = useState<TripDetails | null>(null)
  const status = storedStatus?.trip_id === tripId ? storedStatus : null
  const trip = storedTrip?.id === tripId ? storedTrip : null
  const [errorState, setErrorState] = useState({ tripId, message: '' })
  const loadError = errorState.tripId === tripId ? errorState.message : ''

  useEffect(() => {
    const setLoadError = (message: string) => setErrorState({ tripId, message })
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    let active = true

    async function poll() {
      try {
        const current = await getTripStatus(tripId, controller.signal)
        if (!active) return
        if (current.trip_id !== tripId) throw new ApiError('The server returned a different trip status.', 502)
        setLoadError('')
        setStatus(current)
        if (TERMINAL_STATUSES.has(current.status)) {
          try {
            const details = await getTrip(tripId, controller.signal)
            if (details.id !== tripId) throw new ApiError('The server returned details for a different trip.', 502)
            if (active) setTrip(details)
          } catch (error) {
            if (!controller.signal.aborted) {
              setLoadError(
                error instanceof Error
                  ? error.message
                  : 'Trip details are not available yet.',
              )
              if (!(error instanceof ApiError && error.status !== undefined && error.status >= 400 && error.status < 500 && error.status !== 429)) {
                timer = setTimeout(poll, 7000)
              }
            }
          }
          return
        }
        timer = setTimeout(poll, 3500)
      } catch (error) {
        if (!active || controller.signal.aborted) return
        setLoadError(
          error instanceof Error
            ? error.message
            : 'We could not refresh your trip status.',
        )
        if (error instanceof ApiError && error.status !== undefined && error.status >= 400 && error.status < 500 && error.status !== 429) return
        timer = setTimeout(poll, 7000)
      }
    }

    void poll()
    return () => {
      active = false
      controller.abort()
      if (timer) clearTimeout(timer)
    }
  }, [tripId])

  const currentStatus = status?.status ?? initialStatus
  const activeStage = status?.current_stage?.toLowerCase() || currentStatus
  const activeIndex = Math.max(
    STAGES.findIndex(([key]) => activeStage.includes(key)),
    currentStatus === 'completed' ? STAGES.length : 0,
  )

  return (
    <section className="workspace-panel" aria-live="polite">
      {currentStatus === 'queued' ||
      currentStatus === 'running' ||
      !TERMINAL_STATUSES.has(currentStatus) ? (
        <div className="progress-view">
          <div className="progress-orbit" aria-hidden="true">
            <span>✦</span>
          </div>
          <span className="eyebrow">A LITTLE MAGIC IN MOTION</span>
          <h2>Your trip is taking shape.</h2>
          <p>
            Our planning team is bringing the details together. You can keep
            this page open while we work.
          </p>
          <div
            className="progress-track"
            role="progressbar"
            aria-label="Trip planning progress"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={status?.progress_percentage ?? 0}
          >
            <span
              style={{ width: `${status?.progress_percentage ?? 8}%` }}
            />
          </div>
          <ol className="stage-list">
            {STAGES.map(([key, label], index) => (
              <li
                className={index < activeIndex ? 'stage-done' : index === activeIndex ? 'stage-current' : ''}
                key={key}
              >
                <span aria-hidden="true">{index < activeIndex ? '✓' : index + 1}</span>
                {label}
              </li>
            ))}
          </ol>
          <p className="trip-reference">
            Trip reference <code>{tripId}</code>
          </p>
          {loadError && <p className="inline-note" role="status">{loadError}</p>}
          <button className="button button-secondary" onClick={onStartOver}>Start another trip</button>
        </div>
      ) : currentStatus === 'failed' ? (
        <div className="terminal-state state-error" role="alert">
          <span className="state-icon" aria-hidden="true">!</span>
          <span className="eyebrow">A SMALL DETOUR</span>
          <h2>We couldn’t finish this trip.</h2>
          <p>
            {status?.error_message ||
              'The planning service ran into a problem. Please try again in a little while.'}
          </p>
          {loadError && <p className="inline-note" role="status">{loadError}</p>}
          {trip && <TripResult trip={trip} />}
          <button className="button button-primary" onClick={onStartOver}>
            Try another plan
          </button>
        </div>
      ) : currentStatus === 'insufficient_data' ? (
        <div className="terminal-state">
          <span className="state-icon" aria-hidden="true">?</span>
          <span className="eyebrow">{trip?.days.length ? 'PARTIAL PLAN AVAILABLE' : 'MORE DETAILS NEEDED'}</span>
          <h2>{trip?.days.length ? 'Your itinerary is available for review.' : 'We need a little more to go on.'}</h2>
          <p>
            Some essential details could not be verified, so this plan is not
            approved. Any available itinerary and cost information is shown below.
          </p>
          {status?.error_message && <p className="inline-note" role="status">{status.error_message}</p>}
          {loadError && <p className="inline-note" role="alert">{loadError}</p>}
          {trip && <MissingInformation trip={trip} />}
          {trip && <TripResult trip={trip} />}
          <button className="button button-primary" onClick={onStartOver}>
            Adjust trip details
          </button>
        </div>
      ) : currentStatus === 'revision_required' ? (
        <div className="terminal-state">
          <span className="state-icon" aria-hidden="true">↻</span>
          <span className="eyebrow">A PLAN TO REVIEW</span>
          <h2>Your plan needs a few adjustments.</h2>
          <p>
            This itinerary is not approved yet. Review the notes below before
            relying on any of its details.
          </p>
          {trip && <TripResult trip={trip} />}
          <button className="button button-secondary" onClick={onStartOver}>
            Start a new plan
          </button>
        </div>
      ) : (
        <div className="trip-result-view">
          <div className="success-banner">
            <span aria-hidden="true">✓</span>
            <div>
              <strong>{trip ? 'Your trip details are available.' : 'Loading your trip details.'}</strong>
              <span>
                {trip && !isApproved(trip)
                  ? 'Review the notes below; this plan is not approved.'
                  : 'Here’s a first look at your trip.'}
              </span>
            </div>
          </div>
          {trip ? (
            <TripResult trip={trip} />
          ) : (
            <div className="inline-note" role="status">
              {loadError || 'Your trip details are not available yet.'}
            </div>
          )}
          <button className="button button-secondary" onClick={onStartOver}>
            Plan another trip
          </button>
        </div>
      )}
    </section>
  )
}

function TripResult({ trip }: { trip: TripDetails }) {
  const request = trip.request
  const currency = trip.currency || request.currency
  const budget = trip.budget
  const budgetTotal = budget ? getValue(budget, 'estimated_total') : undefined
  const knownTotal = budgetTotal !== undefined ? budgetTotal : trip.total_estimated_cost
  const knownSubtotal = budget
    ? getValue(budget, 'known_cost_subtotal')
    : undefined
  const remainingBudget = budget
    ? getValue(budget, 'remaining_budget')
    : undefined
  const categoryTotals = budget ? getArray(budget, 'category_totals') : []
  const unpricedComponents = budget
    ? getArray(budget, 'unpriced_components')
    : []
  const lineItems = budget ? getArray(budget, 'line_items') : []
  const hasPricedExpenses = lineItems.some((item) => isRecord(item) && item.amount != null
    && item.currency === currency && Number.isFinite(Number(item.amount)) && Number(item.amount) >= 0)
    || categoryTotals.some((item) => Number(getValue(item, 'known_subtotal')) > 0 && getValue(item, 'currency') === currency)
  const subtotalInconsistent = hasPricedExpenses && (knownSubtotal == null ||
    (Number(knownSubtotal) === 0 && lineItems.some((item) => isRecord(item) && item.currency === currency && Number(item.amount) > 0)))
  const budgetStatus = budget ? getValue(budget, 'status') : undefined
  const pricingStatus = budget ? getValue(budget, 'pricing_status') : undefined
  const budgetIncomplete = knownTotal == null || subtotalInconsistent || pricingStatus === 'INCOMPLETE'
    || budgetStatus === 'INCOMPLETE_ESTIMATE' || budgetStatus === 'INVALID_INPUT'
    || unpricedComponents.length > 0
    || lineItems.some((item) => isRecord(item) && item.amount == null)
    || categoryTotals.some((item) => getValue(item, 'included') !== false && getValue(item, 'complete') === false)
  const sources = collectSources([
    trip.itinerary,
    trip.optimization_review,
    trip.budget,
    trip.accommodation_transport,
    trip.destination_research,
    trip.days.map((day) => day.activities.map((activity) => activity.metadata)),
  ])

  return (
    <div className="trip-result">
      <div className="trip-heading">
        <div>
          <span className="eyebrow">YOUR TRIP AT A GLANCE</span>
          <h2>{trip.title || `${request.origin} to ${request.destination}`}</h2>
          <p>
            {request.start_date} — {request.end_date}
            <span> · </span>
            {request.travelers_count} traveler
            {request.travelers_count === 1 ? '' : 's'}
          </p>
        </div>
        <span className={`review-pill ${isApproved(trip) ? 'approved' : 'review'}`}>
          {isApproved(trip) ? 'Reviewed' : 'Review needed'}
        </span>
      </div>

      <section className="budget-card" aria-label="Budget summary">
        <div className="budget-total">
          <div>
            <span className="eyebrow">ESTIMATED TOTAL</span>
            <strong>{formatMoney(budgetIncomplete ? null : knownTotal, currency)}</strong>
          </div>
          <div>
            <span className="eyebrow">KNOWN COSTS</span>
            <strong>
              {unpricedComponents.length > 0 &&
              Number(knownSubtotal ?? 0) === 0 && !hasPricedExpenses
                ? 'No priced expenses'
                : subtotalInconsistent
                  ? 'See priced expenses below'
                : formatMoney(knownSubtotal, currency)}
            </strong>
          </div>
          <div>
            <span className="eyebrow">YOUR BUDGET</span>
            <strong>{formatMoney(request.total_budget, currency)}</strong>
          </div>
        </div>
        {subtotalInconsistent && <p className="inline-note" role="alert">
          The saved subtotal is missing or inconsistent with priced expenses. Review the individual amounts below; a corrected total has not been inferred.
        </p>}
        <p className="inline-note">
          {!budgetIncomplete && pricingStatus === 'LIVE_PRICE_VERIFIED'
            ? 'Live-price verified budget. Availability may change.'
            : !budgetIncomplete
              ? 'Complete estimated budget. Planning estimates; prices and availability are not live-verified.'
              : 'Incomplete budget. Required costs are unknown; a complete trip total is unavailable.'}
        </p>
        {!budgetIncomplete && budget && getValue(budget, 'minimum_estimated_total') != null && getValue(budget, 'maximum_estimated_total') != null && (
          <p className="inline-note" aria-label="Planning cost range">
            Minimum planning estimate <strong>{formatMoney(getValue(budget, 'minimum_estimated_total'), currency)}</strong>
            {' · '}Maximum planning estimate <strong>{formatMoney(getValue(budget, 'maximum_estimated_total'), currency)}</strong>.
            {' '}Scenario allowances, not guaranteed market bounds.
          </p>
        )}
        {budget && getValue(budget, 'estimate_confidence') != null && (
          <p className="inline-note">Estimate confidence: {humanize(String(getValue(budget, 'estimate_confidence')))}</p>
        )}
        <p className="inline-note">Booking prices and availability remain unverified. Confirm actual fares, hotel availability, tickets and exchange costs before booking.</p>
        {budgetIncomplete && budgetStatus === 'OVER_BUDGET' && budget && getValue(budget, 'over_budget_amount') != null && (
          <p className="remaining-budget" role="alert">Known costs already exceed your budget by <strong>{formatMoney(getValue(budget, 'over_budget_amount'), currency)}</strong>. Unpriced expenses may increase this amount.</p>
        )}
        {!budgetIncomplete && remainingBudget !== undefined && remainingBudget !== null && (
          <p className="remaining-budget">
            {Number(remainingBudget) < 0 ? 'Budget overrun ' : 'Remaining budget '}<strong>{formatMoney(Math.abs(Number(remainingBudget)), currency)}</strong>
          </p>
        )}
        {categoryTotals.length > 0 && (
          <dl className="cost-breakdown">
            {categoryTotals.map((value, index) => (
              <div key={`${getValue(value, 'category')}-${index}`}>
                <dt>{humanize(String(getValue(value, 'category') ?? 'Other'))}</dt>
                <dd>
                  {getValue(value, 'included') === false
                    ? 'Not included'
                    : getValue(value, 'complete') === false &&
                  Number(getValue(value, 'known_subtotal') ?? 0) === 0
                    ? 'No priced amount'
                    : formatMoney(getValue(value, 'known_subtotal'), currency)}
                </dd>
                {getValue(value, 'included') !== false && getValue(value, 'complete') === false && <em>Incomplete</em>}
              </div>
            ))}
          </dl>
        )}
        {lineItems.some(isRecord) && (
          <ul className="budget-line-items">
            {lineItems.map((item, index) =>
              isRecord(item) ? (
                <li key={`budget-item-${index}`}>
                  <span>
                    {String(getValue(item, 'description') ?? 'Cost item')}
                    <small>
                      {costSourceLabel(item)}
                    </small>
                    {typeof getValue(item, 'pricing_basis') === 'string' && (
                      <small>{String(getValue(item, 'pricing_basis'))}</small>
                    )}
                    <CostEvidence item={item} />
                  </span>
                  <strong>
                    {formatMoney(
                      getValue(item, 'amount'),
                      String(getValue(item, 'currency') ?? currency),
                    )}
                  </strong>
                </li>
              ) : null,
            )}
          </ul>
        )}
        {budget && getArray(budget, 'missing_price_actions').length > 0 && (
          <div className="unpriced-list" aria-label="Resolve missing prices">
            <strong>To complete this budget</strong>
            <ul>{getArray(budget, 'missing_price_actions').map((action, index) => <li key={`price-action-${index}`}>{String(action)}</li>)}</ul>
          </div>
        )}
        {(unpricedComponents.length > 0 ||
          lineItems.some(
            (item) => isRecord(item) && getValue(item, 'amount') == null,
          )) && (
          <div className="unpriced-list">
            <strong>Unpriced expenses — not included as free</strong>
            <ul>
              {unpricedComponents.map((item, index) => (
                <li key={`unpriced-${index}`}>{String(item)}</li>
              ))}
              {lineItems.map((item, index) =>
                isRecord(item) && getValue(item, 'amount') == null ? (
                  <li key={`line-${index}`}>
                    {String(getValue(item, 'description') ?? 'Cost not available')}
                  </li>
                ) : null,
              )}
            </ul>
          </div>
        )}
        {budgetStatus === 'OVER_BUDGET' && (
          <p className="budget-alert" role="alert">
            This estimate exceeds the supplied budget. It is not an approved
            plan.
          </p>
        )}
        {budgetStatus === 'INCOMPLETE_ESTIMATE' && (
          <p className="budget-alert" role="status">
            The estimate is incomplete; not all required costs could be priced.
          </p>
        )}
        {budget && (
          <p className="budget-disclaimer">
            Cost estimates and unpriced items are shown as provided by the
            planning service; unknown prices are not assumed to be free.
          </p>
        )}
      </section>

      <StayAndTransport trip={trip} />
      <Warnings trip={trip} />
      {trip.days.length ? (
        <div className="days-list">
          {trip.days.map((day) => (
            <article className="day-card" key={`${day.day_number}-${day.itinerary_date}`}>
              <div className="day-header">
                <div>
                  <span className="eyebrow">DAY {day.day_number}</span>
                  <h3>{day.itinerary_date}</h3>
                </div>
                {day.estimated_daily_cost != null && (
                  <span className="day-cost">
                    {formatMoney(day.estimated_daily_cost, currency)}
                  </span>
                )}
              </div>
              {day.summary && <p className="day-summary">{day.summary.replace(/\s*\[daily cost estimate unknown\]\s*/i, '')}</p>}
              <WeatherNotes itinerary={trip.itinerary} dayNumber={day.day_number} />
              {day.activities.length ? (
                <ol className="activity-list">
                  {day.activities.map((activity, index) => (
                    <ActivityCard
                      activity={activity}
                      currency={currency}
                      key={`${activity.start_time || 'time'}-${activity.title}-${index}`}
                    />
                  ))}
                </ol>
              ) : (
                <p className="inline-note">No activity details are available.</p>
              )}
            </article>
          ))}
        </div>
      ) : (
        <div className="empty-itinerary">
          <span aria-hidden="true">✧</span>
          <h3>Day-by-day details aren’t available yet.</h3>
          <p>We won’t fill in details that weren’t returned by the planner.</p>
        </div>
      )}
      <ReviewNotes review={trip.optimization_review} />
      {trip.days.some((day) => day.activities.some((activity) => getValue(activity.metadata, 'coordinates_verified') === true)) && (
        <p className="inline-note">
          Location data: <a href="https://www.geoapify.com/" target="_blank" rel="noreferrer">Geoapify</a> and{' '}
          <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap contributors</a>.
        </p>
      )}
      {!!sources.length && (
        <section className="source-section">
          <h3>Planning sources</h3>
          <ul>
            {sources.map((source) => (
              <li key={source.url}>
                <a href={source.url} target="_blank" rel="noreferrer">
                  {source.title}
                  <span aria-hidden="true"> ↗</span>
                </a>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}

function StayAndTransport({ trip }: { trip: TripDetails }) {
  const plan = trip.accommodation_transport
  if (!plan) return null
  const accommodation = getValue(plan, 'accommodation')
  const types = isRecord(accommodation) ? getArray(accommodation, 'recommended_types') : []
  const areas = isRecord(accommodation) ? getArray(accommodation, 'stay_area_suggestions') : []
  const transport = getArray(plan, 'transport_options')
  return (
    <section className="day-card" aria-label="Accommodation and transportation">
      <span className="eyebrow">STAY AND GET AROUND</span>
      <h3>Accommodation</h3>
      <p className="inline-note">Recommendations describe planning options. Hotel availability and booking prices are unverified.</p>
      {types.length > 0 && <p>{types.map((value) => humanize(String(value))).join(', ')}</p>}
      {areas.length > 0 && <p>Suggested stay areas: {areas.map(String).join(', ')}</p>}
      {isRecord(accommodation) && <p>
        Rooms: {String(accommodation.room_count ?? 'Unknown')}
        {' · '}Nights: {String(accommodation.nights ?? 'Unknown')}
      </p>}
      <h3>Transportation</h3>
      {transport.length > 0 ? <ul className="budget-line-items">
        {transport.map((option, index) => isRecord(option) ? <li key={`transport-${index}`}>
          <span>
            <strong>{humanize(String(option.mode ?? 'Transport'))}</strong>
            {typeof option.recommendation_reason === 'string' && <small>{option.recommendation_reason}</small>}
            {isRecord(option.route_estimate) ? <small>
              Provider route estimate: {String(option.route_estimate.distance_kilometers)} km
              {' · '}{String(option.route_estimate.duration_minutes)} minutes
              {' · '}{humanize(String(option.route_estimate.provider ?? 'Provider'))}.
              Road routing does not verify flight, rail or bus schedules.
            </small> : <small>No verified route duration or departure schedule is available for this option.</small>}
          </span>
        </li> : null)}
      </ul> : <p className="inline-note">No transport recommendations were returned.</p>}
      <p className="inline-note">Final category prices and their sources appear in the budget breakdown above.</p>
    </section>
  )
}

function ActivityCard({
  activity,
  currency,
}: {
  activity: PersistedActivity
  currency: string
}) {
  const meta = activity.metadata
  const weather = getValue(meta, 'weather') ?? getValue(meta, 'weather_summary')
  const route = getValue(meta, 'route') ?? getValue(meta, 'route_summary')
  const openHours = getValue(meta, 'opening_hours')
  const sourceUrl = getValue(meta, 'source_url')
  const routeMinutes = getValue(meta, 'travel_time_from_previous_minutes')
  const routeVerified = getValue(meta, 'travel_time_verified') === true
  const latitude = getValue(meta, 'latitude')
  const longitude = getValue(meta, 'longitude')
  const coordinatesVerified = getValue(meta, 'coordinates_verified') === true
  const unknownCost = getValue(meta, 'estimated_cost_unknown') === true
  const warnings = getArray(meta, 'warnings')
  return (
    <li className="activity-card">
      <div className="activity-time">
        {activity.start_time || 'Time flexible'}
        {activity.end_time ? ` – ${activity.end_time}` : ''}
      </div>
      <div className="activity-content">
        <div className="activity-title-row">
          <h4>{activity.title}</h4>
          {activity.estimated_cost != null && !unknownCost ? (
            <span>{formatMoney(activity.estimated_cost, currency)}</span>
          ) : unknownCost || activity.estimated_cost == null ? (
            <span className="unknown-cost">Price not available</span>
          ) : null}
        </div>
        {activity.description && <p>{activity.description}</p>}
        {activity.location_name && (
          <p className="activity-meta">⌖ {activity.location_name}</p>
        )}
        {coordinatesVerified &&
          typeof latitude === 'number' &&
          typeof longitude === 'number' && (
            <p className="activity-meta">
              Verified location · {latitude.toFixed(4)}, {longitude.toFixed(4)}
            </p>
          )}
        {activity.category && (
          <span className="activity-category">{humanize(activity.category)}</span>
        )}
        {typeof weather === 'string' && <p className="activity-meta">☼ {weather}</p>}
        {typeof route === 'string' && <p className="activity-meta">↗ {route}</p>}
        {typeof routeMinutes === 'string' && routeVerified && (
          <p className="activity-meta">
            Verified travel time from previous stop · {routeMinutes} min
          </p>
        )}
        {typeof routeMinutes === 'string' && !routeVerified && (
          <p className="activity-meta">Travel time is not verified.</p>
        )}
        {typeof openHours === 'string' && (
          <p className="activity-meta">Opening hours: {openHours}</p>
        )}
        {warnings.map((warning, index) => (
          <p className="activity-warning" key={`warning-${index}`}>
            {String(warning)}
          </p>
        ))}
        {typeof sourceUrl === 'string' && isSafeUrl(sourceUrl) && (
          <a href={sourceUrl} target="_blank" rel="noreferrer">
            Activity source ↗
          </a>
        )}
      </div>
    </li>
  )
}

function WeatherNotes({
  itinerary,
  dayNumber,
}: {
  itinerary: Record<string, unknown> | null
  dayNumber: number
}) {
  const days = itinerary ? getArray(itinerary, 'days') : []
  const day = days.find(
    (item) => isRecord(item) && getValue(item, 'day_number') === dayNumber,
  )
  if (!day || !isRecord(day)) return null
  const condition = getValue(day, 'weather_condition')
  const warning = getValue(day, 'weather_warning')
  const routeNeedsVerification =
    getValue(day, 'requires_route_verification') === true
  if (
    typeof condition !== 'string' &&
    typeof warning !== 'string' &&
    !routeNeedsVerification
  )
    return null
  return (
    <>
      {(typeof condition === 'string' || typeof warning === 'string') && (
        <p className="weather-note">
          <span aria-hidden="true">☼</span>{' '}
          {[condition, warning]
            .filter((value): value is string => typeof value === 'string')
            .join(' · ')}
        </p>
      )}
      {routeNeedsVerification && (
        <p className="activity-warning">
          One or more route durations for this day need verification.
        </p>
      )}
    </>
  )
}

function Warnings({ trip }: { trip: TripDetails | null }) {
  const warnings = trip?.warnings ?? []
  const errors = trip?.errors ?? []
  if (!warnings.length && !errors.length) return null
  return (
    <section className="warnings-card" aria-label="Trip warnings">
      <h3>Worth knowing</h3>
      {[...new Set([...errors, ...warnings])].map((warning, index) => (
        <p key={`${warning}-${index}`}>
          <span aria-hidden="true">i</span> {warning}
        </p>
      ))}
    </section>
  )
}

function MissingInformation({ trip }: { trip: TripDetails }) {
  const missingInputs = trip.budget ? getArray(trip.budget, 'missing_price_inputs').map(String) : []
  const missingCosts = trip.budget
    ? getArray(trip.budget, 'unpriced_components').map(String)
    : []
  const reviewIssues = trip.optimization_review
    ? getArray(trip.optimization_review, 'issues')
    : []
  const actions = reviewIssues.flatMap((issue) =>
    isRecord(issue) && issue.blocks_approval === true
      ? [
          typeof issue.recommended_action === 'string'
            ? issue.recommended_action
            : typeof issue.description === 'string'
              ? issue.description
              : undefined,
        ]
      : [],
  ).filter((value): value is string => Boolean(value))
  if (!missingInputs.length && !missingCosts.length && !actions.length) return null
  return (
    <section className="missing-info" aria-label="Missing information">
      <h3>What’s needed before this plan can be approved</h3>
      <ul>
        {missingInputs.length ? missingInputs.map((field) => (
          <li key={field}>Add: {COST_INPUT_LABELS[field] ?? humanize(field)}{field === 'room_count' ? '' : ` (${trip.currency})`}.</li>
        )) : missingCosts.map((item, index) => (
          <li key={`cost-${index}`}>
            Add a {humanize(item)} planning estimate in your selected currency.
          </li>
        ))}
        {actions.map((action, index) => <li key={`action-${index}`}>{action}</li>)}
      </ul>
    </section>
  )
}

function ReviewNotes({ review }: { review: OptimizationReview | null }) {
  if (!review) return null
  const issues = Array.isArray(review.issues) ? review.issues : []
  const suggestions = Array.isArray(review.suggestions)
    ? review.suggestions
    : []
  return (
    <section className="review-notes">
      <h3>Planner review</h3>
      {typeof review.review_status === 'string' && (
        <p className="review-status-text">
          Status: {humanize(review.review_status)}
        </p>
      )}
      {!!issues.length && (
        <ul>
          {issues.map((issue, index) => (
            <li key={index}>
              {isRecord(issue) && typeof issue.description === 'string'
                ? issue.description
                : 'A planning issue was reported.'}
            </li>
          ))}
        </ul>
      )}
      {!!suggestions.length && (
        <ul>
          {suggestions.map((suggestion, index) => (
            <li key={index}>
              {typeof suggestion === 'string'
                ? suggestion
                : isRecord(suggestion) && typeof suggestion.recommendation === 'string'
                  ? suggestion.recommendation
                  : isRecord(suggestion) && typeof suggestion.description === 'string'
                    ? suggestion.description
                    : 'An improvement was suggested.'}
            </li>
          ))}
        </ul>
      )}
      {!issues.length && !suggestions.length && (
        <p>No additional review notes were returned.</p>
      )}
    </section>
  )
}

function collectSources(values: unknown[]): { title: string; url: string }[] {
  const found = new Map<string, { title: string; url: string }>()
  function visit(value: unknown, depth: number) {
    if (depth > 5 || !value) return
    if (Array.isArray(value)) {
      value.forEach((item) => visit(item, depth + 1))
      return
    }
    if (!isRecord(value)) return
    const url = value.url ?? value.source_url
    if (typeof url === 'string' && isSafeUrl(url)) {
      found.set(url, {
        title:
          (typeof value.title === 'string' && value.title) ||
          (typeof value.name === 'string' && value.name) ||
          'View source',
        url,
      })
    }
    Object.values(value).forEach((item) => visit(item, depth + 1))
  }
  values.forEach((value) => visit(value, 0))
  return [...found.values()]
}

function isApproved(trip: TripDetails): boolean {
  return (
    trip.status === 'completed' &&
    (!trip.optimization_review ||
      getValue(trip.optimization_review, 'approved') === true)
  )
}

function costSourceLabel(item: Record<string, unknown>): string {
  if (item.amount == null || item.classification === 'unknown') return 'Unknown'
  if (item.source === 'verified_provider' && item.classification === 'verified') return 'Live provider price'
  if (item.source === 'user_assumption') return 'User-provided planning estimate'
  if (item.source === 'reference_data') return 'Reference-based planning estimate · not a live quote'
  return 'Non-live planning estimate'
}

function CostEvidence({ item }: { item: Record<string, unknown> }) {
  const sources = getArray(item, 'price_sources')
  const assumptions = getArray(item, 'assumptions')
  const conversion = getValue(item, 'currency_conversion')
  return <>
    {getValue(item, 'minimum_amount') != null && getValue(item, 'maximum_amount') != null && (
      <small>Planning range: {formatMoney(getValue(item, 'minimum_amount'), String(item.currency))} – {formatMoney(getValue(item, 'maximum_amount'), String(item.currency))}</small>
    )}
    {item.confidence != null && <small>Confidence: {humanize(String(item.confidence))}</small>}
    {sources.map((source, index) => {
      const url = getValue(source, 'url')
      return <small key={`cost-source-${index}`}>
        {typeof url === 'string' && isSafeUrl(url)
          ? <a href={url} target="_blank" rel="noreferrer">{String(getValue(source, 'name') ?? 'Price source')}</a>
          : String(getValue(source, 'name') ?? 'Price source')}
        {' · '}Observed {String(getValue(source, 'observed_on') ?? 'date unavailable')}
        {' · '}{String(getValue(source, 'currency') ?? item.currency)}
      </small>
    })}
    {assumptions.map((assumption, index) => <small key={`cost-assumption-${index}`}>Assumption: {String(assumption)}</small>)}
    {isRecord(conversion) && <small>
      Reference FX: {String(conversion.original_currency)} → {String(conversion.target_currency)} at {String(conversion.rate)}, dated {String(conversion.rate_date)}.
      {' '}{String(conversion.source)}. Retail fees are not included.
    </small>}
  </>
}

function getArray(value: Record<string, unknown>, key: string): unknown[] {
  const item = value[key]
  return Array.isArray(item) ? item : []
}

function getValue(
  value: unknown,
  key: string,
): unknown {
  return isRecord(value) ? value[key] : undefined
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isSafeUrl(value: string): boolean {
  try {
    return new URL(value).protocol === 'https:'
  } catch {
    return false
  }
}

function formatMoney(value: unknown, currency: string): string {
  if (value === undefined || value === null || value === '') return 'Not available'
  const amount = Number(value)
  if (!Number.isFinite(amount)) return 'Not available'
  try {
    return new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency,
      maximumFractionDigits: 2,
    }).format(amount)
  } catch {
    return `${currency} ${amount.toFixed(2)}`
  }
}

function humanize(value: string): string {
  return value
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (character) => character.toUpperCase())
}
