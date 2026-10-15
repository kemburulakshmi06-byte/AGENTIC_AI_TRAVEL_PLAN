import { useEffect, useRef, useState } from 'react'
import { ApiError, getTrip, getTripStatus, reviseTrip } from '../api'
import { COST_INPUT_LABELS } from '../trip-request-validation'
import type {
  BudgetLineItem,
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
  const [pollVersion, setPollVersion] = useState(0)
  const [revisionBusy, setRevisionBusy] = useState(false)
  const [revisionError, setRevisionError] = useState('')
  const [awaitingRevisionTrip, setAwaitingRevisionTrip] = useState<string | null>(null)
  const [allowEconomy, setAllowEconomy] = useState(false)
  const revisionLocks = useRef(new Set<string>())
  const activeTripId = useRef(tripId)
  activeTripId.current = tripId
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  useEffect(() => { setRevisionBusy(false); setRevisionError(''); setAllowEconomy(false); setAwaitingRevisionTrip(null) }, [tripId])
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
            if (active) {
              setTrip(details)
              setAwaitingRevisionTrip((pending) => pending === tripId ? null : pending)
            }
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
  }, [tripId, pollVersion])

  async function requestRevision() {
    if (revisionLocks.current.has(tripId)) return
    revisionLocks.current.add(tripId)
    setRevisionBusy(true)
    setRevisionError('')
    const requestedId = tripId
    try {
      const accepted = await reviseTrip(tripId, allowEconomy)
      if (!mounted.current || activeTripId.current !== requestedId) return
      if (accepted.trip_id !== requestedId) throw new ApiError('The server returned a revision for a different trip.', 502)
      setAwaitingRevisionTrip(tripId)
      setStatus({ trip_id: tripId, task_id: tripId, status: 'queued', current_stage: 'budget_revision',
        progress_percentage: 0, message: accepted.message, error_message: null,
        created_at: trip?.created_at ?? '', updated_at: trip?.updated_at ?? '' })
      setPollVersion((version) => version + 1)
    } catch (error) {
      if (mounted.current && activeTripId.current === requestedId) {
        setRevisionError(error instanceof Error ? error.message : 'The revision could not be requested.')
        // A lost POST response can still mean a committed job. Reconcile via GET;
        // never automatically repeat a mutation.
        setPollVersion((version) => version + 1)
      }
    } finally {
      revisionLocks.current.delete(requestedId)
      if (mounted.current && activeTripId.current === requestedId) setRevisionBusy(false)
    }
  }

  const currentStatus = status?.status ?? initialStatus
  const resultTrip = trip ? { ...trip, status: currentStatus } : null
  const revisionState = resultTrip ? getRevisionState(resultTrip) : 'none'
  const loadingRevision = awaitingRevisionTrip === tripId && TERMINAL_STATUSES.has(currentStatus)
  const activeStage = status?.current_stage?.toLowerCase() || currentStatus
  const revisionLabel = activeStage === 'budget_revision' ? 'Revising budget'
    : activeStage === 'revision' ? 'Rechecking itinerary' : null
  const revising = activeStage === 'budget_revision' || activeStage === 'revision'
    || pollVersion > 0 || !!trip?.budget_revision_summary?.attempts
  const hasRevision = revisionState !== 'none'
  const stageKey = activeStage === 'budget_revision' ? 'budget' : activeStage === 'revision' ? 'itinerary' : activeStage
  const activeIndex = Math.max(
    STAGES.findIndex(([key]) => stageKey.includes(key)),
    currentStatus === 'completed' ? STAGES.length : 0,
  )

  return (
    <section className={`workspace-panel ${trip ? 'dashboard-workspace' : ''}`} aria-live="polite">
      {resultTrip && !loadingRevision && !['queued', 'running'].includes(currentStatus) && <BudgetRevisionControl trip={resultTrip}
        busy={revisionBusy} error={revisionError} allowEconomy={allowEconomy} onEconomyChange={setAllowEconomy}
        onRevise={() => void requestRevision()} />}
      {loadingRevision ? (
        <div className="terminal-state">
          <h2>{currentStatus === 'failed' ? 'Budget revision failed' : 'Loading your revision result'}</h2>
          <p role="status">{status?.error_message || status?.message || 'Retrieving the saved budget and reviewer outcome.'}</p>
          {loadError && <p className="inline-note" role="alert">{loadError}</p>}
        </div>
      ) : currentStatus === 'queued' ||
      currentStatus === 'running' ||
      !TERMINAL_STATUSES.has(currentStatus) ? (
        <div className="progress-view">
          <div className="progress-orbit" aria-hidden="true">
            <span>✦</span>
          </div>
          <span className="eyebrow">A LITTLE MAGIC IN MOTION</span>
          {revising && <span className="plan-status" aria-label="Plan status">Optimizing</span>}
          <h2>{revising ? 'Optimizing your travel plan...' : 'Your trip is taking shape.'}</h2>
          {revisionLabel && <p className="inline-note" aria-label="Current revision">{revisionLabel}</p>}
          {status?.message && <p className="inline-note" aria-label="Planning status message">{status.message}</p>}
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
              style={{ width: `${status?.progress_percentage ?? 0}%` }}
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
          <h2>{hasRevision ? 'Budget revision failed' : 'We couldn’t finish this trip.'}</h2>
          <p>
            {status?.error_message ||
              (hasRevision ? 'The revision could not be completed. Review the saved outcome below.'
                : 'The planning service ran into a problem. Please try again in a little while.')}
          </p>
          {loadError && <p className="inline-note" role="status">{loadError}</p>}
          {trip && <TripResult trip={{ ...trip, status: currentStatus }} onReviewOptions={onStartOver} />}
          {(!hasRevision || !trip?.budget_revision_summary?.history.length) && <button className="button button-primary" onClick={onStartOver}>
            Try another plan
          </button>}
        </div>
      ) : currentStatus === 'insufficient_data' ? (
        <div className="terminal-state">
          <span className="state-icon" aria-hidden="true">?</span>
          <span className="eyebrow">{trip?.days.length ? 'PARTIAL PLAN AVAILABLE' : 'MORE DETAILS NEEDED'}</span>
          <h2>{hasRevision ? 'Budget review is incomplete' : trip?.days.length ? 'Your itinerary is available for review.' : 'We need a little more to go on.'}</h2>
          <p>
            Some essential details could not be verified, so this plan is not
            approved. Any available itinerary and cost information is shown below.
          </p>
          {status?.error_message && <p className="inline-note" role="status">{status.error_message}</p>}
          {loadError && <p className="inline-note" role="alert">{loadError}</p>}
          {trip && <TripResult trip={{ ...trip, status: currentStatus }} onReviewOptions={onStartOver} />}
          {(!hasRevision || !trip?.budget_revision_summary?.history.length) && <button className="button button-primary" onClick={onStartOver}>
            Adjust trip details
          </button>}
        </div>
      ) : currentStatus === 'revision_required' ? (
        <div className="terminal-state">
          <span className="state-icon" aria-hidden="true">↻</span>
          <span className="eyebrow">{hasRevision ? 'YOUR REVISED PLAN' : 'A PLAN TO REVIEW'}</span>
          <h2>{revisionState === 'failed' ? 'Budget revision failed'
            : revisionState === 'incomplete' ? 'Budget review is incomplete'
            : revisionState === 'unconfirmed' ? 'Revision outcome unavailable'
            : hasRevision ? 'Budget Review Completed' : 'Your plan needs a few adjustments.'}</h2>
          {!hasRevision && <p>
            This itinerary is not approved yet. Review the notes below before
            relying on any of its details.
          </p>}
          {status?.error_message && <p className="inline-note" role="status">{status.error_message}</p>}
          {loadError && <p className="inline-note" role="alert">{loadError}</p>}
          {!trip && !loadError && <p className="inline-note" role="status">Loading your trip details.</p>}
          {trip && <TripResult trip={{ ...trip, status: currentStatus }} onReviewOptions={onStartOver} />}
          {(!hasRevision || !trip?.budget_revision_summary?.history.length) && <button className="button button-secondary" onClick={onStartOver}>
            Start a new plan
          </button>}
        </div>
      ) : (
        <div className="trip-result-view">
          <div className="success-banner">
            <span aria-hidden="true">✦</span>
            <div>
              <strong>{hasRevision ? revisionState === 'failed' ? 'Budget revision failed'
                : revisionState === 'unconfirmed' ? 'Revision outcome unavailable'
                : revisionState === 'incomplete' ? 'Budget review is incomplete' : 'Budget Review Completed'
                : trip ? 'Your trip details are available.' : 'Loading your trip details.'}</strong>
              <span>
                {hasRevision ? 'Review your saved revision outcome below.' : trip && !isApproved(trip)
                  ? 'Review the notes below; this plan is not approved.'
                  : 'Here’s a first look at your trip.'}
              </span>
            </div>
          </div>
          {trip ? (
            <TripResult trip={{ ...trip, status: currentStatus }} onReviewOptions={onStartOver} />
          ) : (
            <div className="inline-note" role="status">
              {loadError || 'Your trip details are not available yet.'}
            </div>
          )}
          {(!hasRevision || revisionState === 'optimized' || !trip?.budget_revision_summary?.history.length) && <button className="button button-secondary" onClick={onStartOver}>
            Plan another trip
          </button>}
        </div>
      )}
    </section>
  )
}

function isBudgetIncomplete(trip: TripDetails): boolean {
  const budget = trip.budget
  const currency = trip.request.currency
  const total = budget?.estimated_total !== undefined ? budget.estimated_total : trip.total_estimated_cost
  const lines = budget?.line_items ?? []
  const categories = budget?.category_totals ?? []
  const hasPricedExpenses = lines.some((item) => moneyValue(item.amount) != null && Number(item.amount) >= 0 && item.currency === currency)
    || categories.some((item) => Number(item.known_subtotal) > 0 && item.currency === currency)
  const subtotalInconsistent = hasPricedExpenses && (budget?.known_cost_subtotal == null
    || (Number(budget.known_cost_subtotal) === 0 && lines.some((item) => item.currency === currency && Number(item.amount) > 0)))
  return budgetHasConflictingFigures(trip) || moneyValue(total) == null || Number(total) < 0 || subtotalInconsistent
    || trip.currency !== currency || (budget?.currency != null && budget.currency !== currency)
    || budget?.pricing_status === 'INCOMPLETE' || budget?.status === 'INCOMPLETE_ESTIMATE' || budget?.status === 'INVALID_INPUT'
    || !!budget?.unpriced_components?.length
    || lines.some((item) => moneyValue(item.amount) == null || Number(item.amount) < 0 || item.currency !== currency
      || hasRejectedFlightReference(item, trip))
    || categories.some((item) => item.included !== false && item.complete === false)
}

type RevisionState = 'none' | 'running' | 'optimized' | 'over_budget' | 'exhausted' | 'failed' | 'incomplete' | 'awaiting_review' | 'unconfirmed'

function getRevisionState(trip: TripDetails, incomplete = isBudgetIncomplete(trip)): RevisionState {
  const summary = trip.budget_revision_summary
  const latest = summary?.history.at(-1)
  if (!latest) return summary?.attempts ? 'unconfirmed' : 'none'
  if (trip.status === 'queued' || trip.status === 'running') return 'running'
  if (trip.status === 'failed' || latest.outcome === 'failed') return 'failed'
  if (incomplete || trip.status === 'insufficient_data' || latest.outcome === 'incomplete') return 'incomplete'
  const total = moneyValue(trip.budget?.estimated_total)
  const original = moneyValue(trip.request.total_budget)
  // A historical result must describe the current saved budget, not an earlier attempt.
  if (summary?.attempts !== latest.attempt || latest.currency !== trip.request.currency
    || total == null || original == null || moneyValue(latest.revised_total) !== total
    || (latest.original_budget != null && moneyValue(latest.original_budget) !== original)) return 'unconfirmed'
  if (latest.outcome === 'within_budget' && total <= original) return isApproved(trip) ? 'optimized' : 'awaiting_review'
  if (total > original && (latest.outcome === 'no_supported_alternative'
    || (latest.outcome === 'improved' && moneyValue(latest.savings) != null && Number(latest.savings) > 0))) {
    return latest.alternatives_exhausted === true ? 'exhausted' : 'over_budget'
  }
  return 'unconfirmed'
}

function TripResult({ trip, onReviewOptions }: { trip: TripDetails; onReviewOptions: () => void }) {
  const request = trip.request
  const currency = request.currency
  const budget = trip.budget
  const budgetTotal = budget ? getValue(budget, 'estimated_total') : undefined
  const knownTotal = budgetTotal !== undefined ? budgetTotal : trip.total_estimated_cost
  const knownSubtotal = budget
    ? getValue(budget, 'known_cost_subtotal')
    : undefined
  const remainingBudget = budget
    ? getValue(budget, 'remaining_budget')
    : undefined
  const originalLineItems = budget ? getArray(budget, 'line_items') : []
  const rejectedFlightCost = originalLineItems.some((item) => hasRejectedFlightReference(item, trip))
  const categoryTotals = (budget ? getArray(budget, 'category_totals') : []).map((item) =>
    rejectedFlightCost && isRecord(item) && item.category === 'intercity_transport'
      ? { ...item, complete: false, known_subtotal: null } : item)
  const unpricedComponents = budget
    ? getArray(budget, 'unpriced_components')
    : []
  const lineItems = originalLineItems.map((item) => hasRejectedFlightReference(item, trip) && isRecord(item)
    ? { ...item, amount: null, classification: 'unknown', minimum_amount: null, maximum_amount: null, pricing_basis: null } : item)
  const verifiedPriceBudget = pricingIsVerified(budget, lineItems)
  const hasPricedExpenses = lineItems.some((item) => isRecord(item) && item.amount != null
    && item.currency === currency && Number.isFinite(Number(item.amount)) && Number(item.amount) >= 0)
    || categoryTotals.some((item) => Number(getValue(item, 'known_subtotal')) > 0 && getValue(item, 'currency') === currency)
  const subtotalInconsistent = rejectedFlightCost || (hasPricedExpenses && (knownSubtotal == null ||
    (Number(knownSubtotal) === 0 && lineItems.some((item) => isRecord(item) && item.currency === currency && Number(item.amount) > 0))))
  const budgetStatus = budget ? getValue(budget, 'status') : undefined
  const conflictingFigures = budgetHasConflictingFigures(trip)
  const currencyMismatch = trip.currency !== currency || (budget?.currency != null && budget.currency !== currency)
  const budgetIncomplete = isBudgetIncomplete(trip)
  const approved = isApproved(trip) && !budgetIncomplete && budgetStatus !== 'OVER_BUDGET'
  const revisionState = getRevisionState(trip, budgetIncomplete)
  const reviewedOverBudget = revisionState === 'over_budget' || revisionState === 'exhausted'
  const sources = collectSources([
    trip.itinerary,
    trip.optimization_review,
    trip.budget,
    trip.accommodation_transport,
    trip.destination_research,
    trip.days.map((day) => day.activities.map((activity) => activity.metadata)),
  ])

  return (
    <div className="trip-result results-dashboard">
      <header className="results-hero">
        <div className="results-hero-top">
          <span className="eyebrow">YOUR PERSONAL TRAVEL BLUEPRINT</span>
          <span className="plan-status" aria-label="Plan status">{reviewedOverBudget ? 'Review Completed'
            : revisionState === 'optimized' && approved ? 'Approved' : humanize(trip.status)}</span>
        </div>
        <h2>{revisionState === 'optimized' ? 'Budget Optimized Successfully'
          : revisionState === 'over_budget' || revisionState === 'exhausted' ? 'Best Supported Plan — Over Budget'
          : revisionState === 'failed' ? 'Budget Revision Failed'
          : revisionState === 'unconfirmed' ? 'Revision Outcome Unavailable'
          : revisionState === 'awaiting_review' ? 'Budget Review Completed'
          : trip.status === 'failed' ? 'Your Travel Planning Summary'
          : trip.days.length && budgetStatus === 'OVER_BUDGET' && !budgetIncomplete
          ? 'Your Travel Plan Needs Budget Optimization'
          : trip.days.length && budgetIncomplete ? 'Your Personalized Itinerary Is Ready'
          : trip.days.length && !budgetIncomplete ? 'Your AI Travel Plan Is Ready!' : 'Your Travel Planning Summary'}</h2>
        <div className="destination-heading">
          <h3>{request.destination}</h3>
          <span className={`review-pill ${approved ? 'approved' : 'review'}`}>
            {approved ? 'Reviewed' : reviewedOverBudget ? 'Over Budget' : 'Review needed'}
          </span>
        </div>
        <p className="saved-plan-title">{trip.title || `${request.origin} to ${request.destination}`}</p>
        <dl className="trip-facts">
          <div><dt>Departing from</dt><dd>{request.origin}</dd></div>
          <div><dt>Travel dates</dt><dd><time dateTime={request.start_date}>{formatDate(request.start_date)}</time> — <time dateTime={request.end_date}>{formatDate(request.end_date)}</time></dd></div>
          <div><dt>Duration</dt><dd>{tripDuration(request.start_date, request.end_date)}</dd></div>
          <div><dt>Travelers</dt><dd>{request.travelers_count} {request.travelers_count === 1 ? 'traveler' : 'travelers'}</dd></div>
          <div><dt>Travel style</dt><dd>{request.travel_style ? humanize(request.travel_style) : 'Not specified'}</dd></div>
        </dl>
        <p className="hero-footnote">A planning estimate for your journey. Booking readiness remains unverified.</p>
      </header>

      <section className="budget-card" aria-label="Budget summary">
        <div className="dashboard-section-heading"><div><span className="eyebrow">THE FINANCIAL PICTURE</span><h3>Your trip, in numbers</h3></div><span className="currency-label">Amounts in {currency}</span></div>
        <div className="budget-total">
          <div aria-label="Estimated total cost">
            <span className="eyebrow">ESTIMATED TOTAL</span>
            <strong>{formatMoney(budgetIncomplete ? null : knownTotal, currency)}</strong>
          </div>
          <div aria-label="Known-cost subtotal">
            <span className="eyebrow">KNOWN COSTS</span>
            {budgetIncomplete && <small className="metric-caption">Incomplete subtotal</small>}
            <strong>
              {unpricedComponents.length > 0 &&
              Number(knownSubtotal ?? 0) === 0 && !hasPricedExpenses
                ? 'No priced expenses'
                : subtotalInconsistent
                  ? 'See priced expenses below'
                : formatMoney(knownSubtotal, budget?.currency || trip.currency || currency)}
            </strong>
          </div>
          <div aria-label="Original budget">
            <span className="eyebrow">YOUR BUDGET</span>
            <strong>{formatMoney(request.total_budget, request.currency)}</strong>
          </div>
          <div aria-label="Budget balance" className={!budgetIncomplete && Number(remainingBudget) < 0 ? 'metric-overrun' : ''}>
            <span className="eyebrow">{!budgetIncomplete && Number(remainingBudget) < 0 ? 'AMOUNT OVER BUDGET' : 'REMAINING BUDGET'}</span>
            <strong>{formatMoney(budgetIncomplete || moneyValue(remainingBudget) == null ? null : Math.abs(Number(remainingBudget)), currency)}</strong>
            {budgetIncomplete && <small className="metric-caption">Requires complete pricing</small>}
          </div>
        </div>
        <BudgetUtilization total={budgetIncomplete ? null : knownTotal} originalBudget={request.total_budget} />
        {lineItems.some((item) => isRecord(item) && item.ai_provenance != null) && <p className="ai-estimate-notice" role="note">
          <span aria-hidden="true">✦</span> AI-generated prices are planning estimates, not live booking quotes. Actual costs may vary.
        </p>}
        {subtotalInconsistent && <p className="inline-note" role="alert">
          {rejectedFlightCost
            ? 'Flight evidence does not apply to this trip and has been excluded from displayed costs. The saved total cannot be used; a corrected total has not been inferred.'
            : 'The saved subtotal is missing or inconsistent with priced expenses. Review the individual amounts below; a corrected total has not been inferred.'}
        </p>}
        {conflictingFigures && <p className="inline-note" role="alert">The saved budget figures do not reconcile with the original request or cost breakdown. A corrected total has not been inferred; review the returned amounts.</p>}
        {currencyMismatch && <p className="inline-note" role="alert">Returned pricing uses a different currency from your original budget. A complete total requires supported currency conversion; no conversion has been inferred.</p>}
        <p className="inline-note">
          {!budgetIncomplete && verifiedPriceBudget
            ? 'Verified-price budget. Booking prices and availability still require confirmation.'
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
        <CategoryBreakdown categories={categoryTotals} currency={currency} originalBudget={request.total_budget} />
        {lineItems.some(isRecord) && (
          <details className="cost-evidence-details">
          <summary>Cost assumptions & evidence <span>See the basis for each expense</span></summary>
          <ul className="budget-line-items">
            {lineItems.map((item, index) =>
              isRecord(item) ? (
                <li key={`budget-item-${index}`}>
                  <div className="cost-item-details">
                    <span className="cost-item-title">{String(getValue(item, 'description') ?? 'Cost item')}</span>
                    <small className="evidence-label">
                      {costSourceLabel(item)}
                    </small>
                    {typeof getValue(item, 'pricing_basis') === 'string' && (
                      <small>{String(getValue(item, 'pricing_basis'))}</small>
                    )}
                    <CostEvidence item={item} />
                  </div>
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
          </details>
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

      <RevisionResults trip={trip} incomplete={budgetIncomplete} onReviewOptions={onReviewOptions} />

      <section className="itinerary-section" aria-label="Day-by-day itinerary">
      <div className="dashboard-section-heading"><div><span className="eyebrow">MAKE EVERY DAY COUNT</span><h3>Your day-by-day journey</h3></div><span className="section-caption">{trip.days.length} {trip.days.length === 1 ? 'day' : 'days'} returned by the planner</span></div>
      {trip.days.length ? (
        <div className="days-list">
          {trip.days.map((day) => (
            <article className="day-card itinerary-day" aria-label={`Day ${day.day_number} itinerary`} key={`${day.day_number}-${day.itinerary_date}`}>
              <div className="day-header">
                <div>
                  <span className="eyebrow">DAY {day.day_number}</span>
                  <h4><time dateTime={day.itinerary_date}>{formatDate(day.itinerary_date)}</time></h4>
                </div>
                {day.estimated_daily_cost != null && (
                  <span className="day-cost">Daily planning allocation<br />
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
      </section>
      {budgetIncomplete && <MissingInformation trip={trip} />}
      <OptimizationSummary trip={trip} incomplete={budgetIncomplete} />
      <div className="travel-logistics-grid">
        <StayAndTransport trip={trip} />
        <FlightPricingEvidence trip={trip} />
      </div>
      <Warnings trip={trip} />
      <ReviewNotes trip={trip} approved={approved} />
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

const CATEGORY_LABELS: Record<string, string> = {
  accommodation: 'Accommodation', intercity_transport: 'Round-trip intercity travel',
  local_transport: 'Local transportation', food: 'Food', activities: 'Activities',
  contingency: 'Contingency', other: 'Other expenses',
}

function CategoryBreakdown({ categories, currency, originalBudget }: { categories: unknown[]; currency: string; originalBudget: string }) {
  const supplied = categories.filter(isRecord)
  const rows = Object.keys(CATEGORY_LABELS).filter((category) => category !== 'other'
    || supplied.some((row) => row.category === 'other')).map((category) => ({
      category, value: supplied.find((row) => row.category === category),
    }))
  const denominator = moneyValue(originalBudget)
  return <div className="category-dashboard" aria-label="Category cost breakdown">
    <div className="dashboard-section-heading"><h4>Where your budget goes</h4><span className="section-caption">Bars compare each cost with your original budget</span></div>
    <dl className="cost-breakdown">
      {rows.map(({ category, value }) => {
        const amount = moneyValue(value?.known_subtotal)
        const comparable = value != null && value.included !== false && amount != null && amount >= 0
          && (value.currency == null || value.currency === currency) && !(value.complete === false && amount === 0)
        const percent = comparable && denominator != null && denominator > 0 ? amount / denominator * 100 : null
        return <div key={category} className={`category-row category-${category}`}>
          <dt>{CATEGORY_LABELS[category]}</dt>
          <dd>{value?.included === false ? 'Not included' : !value ? 'Not supplied'
            : !comparable ? 'No priced amount' : formatMoney(amount, currency)}</dd>
          {value?.included !== false && value?.complete === false && <em>Incomplete</em>}
          {percent != null ? <div className="category-meter" role="meter" aria-label={`${CATEGORY_LABELS[category]} share of original budget`}
            aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.min(100, percent)} aria-valuetext={`${percent.toFixed(1)}% of original budget`}>
            <span style={{ width: `${Math.min(100, percent)}%` }} />
          </div> : <div className="category-meter unavailable-meter" aria-hidden="true" />}
        </div>
      })}
    </dl>
  </div>
}

function BudgetUtilization({ total, originalBudget }: { total: unknown; originalBudget: string }) {
  const amount = moneyValue(total)
  const budget = moneyValue(originalBudget)
  if (amount == null || amount < 0 || budget == null || budget <= 0) return null
  const percent = amount / budget * 100
  return <div className={`budget-utilization ${amount > budget ? 'utilization-over' : ''}`}>
    <p><span>Estimated budget use</span><strong>{percent.toFixed(1)}% of your original budget</strong></p>
    <div className="utilization-track" role="meter" aria-label="Estimated budget use" aria-valuemin={0} aria-valuemax={100}
      aria-valuenow={Math.min(100, percent)} aria-valuetext={`${percent.toFixed(1)}% of original budget`}>
      <span style={{ width: `${Math.min(100, percent)}%` }} />
    </div>
  </div>
}

function BudgetRevisionControl({ trip, busy, error, allowEconomy, onEconomyChange, onRevise }: {
  trip: TripDetails; busy: boolean; error: string; allowEconomy: boolean
  onEconomyChange: (value: boolean) => void; onRevise: () => void
}) {
  const budget = trip.budget
  const total = moneyValue(budget?.estimated_total)
  const original = moneyValue(trip.request.total_budget)
  const incomplete = !budget || budget.pricing_status === 'INCOMPLETE' || budget.status === 'INCOMPLETE_ESTIMATE'
    || budget.status === 'INVALID_INPUT' || budgetHasConflictingFigures(trip)
    || budget.currency !== trip.request.currency || trip.currency !== trip.request.currency
    || !!budget.unpriced_components?.length
    || budget.category_totals?.some((category) => category.included !== false && category.complete === false)
    || budget.line_items?.some((item) => moneyValue(item.amount) == null || Number(item.amount) < 0
      || item.currency !== trip.request.currency || hasRejectedFlightReference(item, trip))
  if (incomplete || total == null || original == null || total <= original
    || (!['completed', 'revision_required'].includes(trip.status)
      && !(trip.status === 'failed' && trip.budget_revision_summary?.history.at(-1)?.outcome === 'failed'))) return null
  const summary = trip.budget_revision_summary
  if (summary?.can_retry === false) return <section className="day-card" aria-label="Revise budget">
    <button className="button button-secondary" disabled>Revise Plan to Fit Budget</button>
    <p className="inline-note">No further revision is eligible.</p>
    {error && <p role="alert">{error}</p>}
  </section>
  const eligible = !summary?.requires_economy_permission || allowEconomy
  return <section className="day-card" aria-label="Revise budget">
    <h3>Bring your plan closer to your budget</h3>
    <p>Further savings needed: <strong>{formatMoney(total - original, trip.request.currency)}</strong>.</p>
    <p className="inline-note">We’ll compare supported alternatives while preserving your saved trip and fixed prices. A lower total is not guaranteed.</p>
    {summary?.economy_accommodation_available !== false && <label><input type="checkbox" checked={allowEconomy} disabled={busy}
      onChange={(event) => onEconomyChange(event.target.checked)} /> Allow economy accommodation if it changes my preferred accommodation tier</label>}
    <p><button className="button button-primary" disabled={busy || !eligible} onClick={onRevise}>
      {busy ? 'Optimizing your travel plan...' : 'Revise Plan to Fit Budget'}
    </button></p>
    {summary?.requires_economy_permission && !allowEconomy && <p className="inline-note">Another attempt requires permission to consider economy accommodation.</p>}
    {error && <p role="alert">{error}</p>}
  </section>
}

function RevisionResults({ trip, incomplete, onReviewOptions }: {
  trip: TripDetails; incomplete: boolean; onReviewOptions: () => void
}) {
  const history = trip.budget_revision_summary?.history ?? []
  const latest = history.at(-1)
  if (!latest) return null
  const unavailable = incomplete || trip.status === 'insufficient_data' || latest.outcome === 'incomplete' || latest.currency !== trip.request.currency
  const savings = latest.cumulative_savings ?? latest.savings
  const state = getRevisionState(trip, incomplete)
  const successful = state === 'optimized'
  const explanation = state === 'failed' ? 'The revision could not be completed. Review the error before retrying.'
    : state === 'incomplete' ? 'Required costs or applicable evidence are missing; affordability cannot be confirmed.'
    : state === 'unconfirmed' ? 'The saved revision outcome could not be confirmed. Review the details before relying on this result.'
    : state === 'optimized' ? 'The revised planning estimate fits your original budget and has explicit reviewer approval.'
    : state === 'awaiting_review' ? 'The estimate fits your budget; reviewer approval is not confirmed.'
    : state === 'exhausted' ? 'No further compatible savings available with current pricing information.'
    : 'Supported alternatives were evaluated; the estimate remains above your original budget.'
  const finalTotal = unavailable ? null : trip.budget?.estimated_total
  const overrun = unavailable ? null : trip.budget?.over_budget_amount ?? latest.remaining_over_budget
  return <section className="day-card revision-outcome-card" aria-label="Budget revision results">
    <h3>Revision outcome</h3>
    <dl className="revision-outcome-metrics">
      <div><dt>Original budget</dt><dd>{formatMoney(trip.request.total_budget, trip.request.currency)}</dd></div>
      <div><dt>Final estimated cost</dt><dd>{formatMoney(finalTotal, trip.request.currency)}</dd></div>
      <div><dt>{latest.cumulative_savings == null && history.length > 1 ? 'Savings this attempt' : 'Actual savings achieved'}</dt>
        <dd>{formatMoney(unavailable || state === 'failed' || state === 'unconfirmed' ? null : savings, trip.request.currency)}</dd></div>
      <div><dt>Remaining overrun</dt><dd>{formatMoney(overrun, trip.request.currency)}</dd></div>
    </dl>
    <p aria-label="Revision outcome explanation">{explanation}</p>
    {!successful && <div className="revision-next-steps" aria-label="Revision next steps">
      <strong>Next steps</strong>
      <ul>
        <li>{unavailable ? 'Supply the missing costs or applicable travel prices.' : 'Review a higher budget or supply new supported travel prices.'}</li>
        {trip.budget_revision_summary?.can_retry && trip.budget_revision_summary.economy_accommodation_available && <li>Approve economy accommodation using the revision options above.</li>}
      </ul>
      <button className="button button-secondary" onClick={onReviewOptions}>Review budget or add prices</button>
      <p className="inline-note">Changes require your review and a new submission. This saved trip’s budget, dates, travelers, and destination stay unchanged.</p>
    </div>}
    <p className="inline-note">Booking readiness remains unverified. AI estimates are not live fares.</p>
    <details className="result-details">
      <summary>View revision details</summary>
      {history.map((record) => <div key={record.attempt}>
        <h4>Attempt {record.attempt}</h4>
        <p>Before: {formatMoney(record.original_total, record.currency)} · After: {formatMoney(record.revised_total, record.currency)}</p>
        {moneyValue(record.savings) != null && Number(record.savings) >= 0 && <p>Actual estimate reduction: {formatMoney(record.savings, record.currency)}</p>}
        {moneyValue(record.remaining_over_budget) != null && Number(record.remaining_over_budget) > 0
          && <p>Still over budget: {formatMoney(record.remaining_over_budget, record.currency)}</p>}
        <p>{record.explanation}</p>
        {!!record.changes.length && <ul>{record.changes.map((change) => <li key={change}>{change}</li>)}</ul>}
      </div>)}
      <p>Planning approval depends on the reviewer. Booking readiness remains unverified.</p>
    </details>
  </section>
}

function OptimizationSummary({ trip, incomplete }: { trip: TripDetails; incomplete: boolean }) {
  const budget = trip.budget
  const review = trip.optimization_review
  const original = moneyValue(trip.request.total_budget)
  const total = incomplete ? null : moneyValue(budget?.estimated_total ?? trip.total_estimated_cost)
  const affordable = total != null && original != null && total <= original && budget?.status !== 'OVER_BUDGET'
  const revisionState = getRevisionState(trip, incomplete)
  const reviewedOverBudget = revisionState === 'over_budget' || revisionState === 'exhausted'
  // Retain workflow evidence for older trips without interactive revision history.
  const revisionWarnings = [...new Set(trip.warnings.filter((warning) =>
    /Applied supported budget alternatives|Applied a validated itinerary-only revision/i.test(warning)))]
  const savings = moneyValue(review?.estimated_savings)
  const supportedSuggestions = review?.suggestions?.filter((suggestion) =>
    moneyValue(suggestion.estimated_savings) != null && Number(suggestion.estimated_savings) > 0
    && suggestion.currency === trip.request.currency && suggestion.savings_basis?.trim()) ?? []
  const supportedCents = supportedSuggestions.reduce((sum, suggestion) => sum + Math.round(Number(suggestion.estimated_savings) * 100), 0)
  const showSavings = savings != null && savings > 0 && review?.currency === trip.request.currency
    && Math.round(savings * 100) === supportedCents
  return <section className="optimization-summary" aria-label="Optimization summary">
    <div className="dashboard-section-heading"><div><span className="eyebrow">A THOUGHTFUL SECOND LOOK</span><h3>Budget & optimization</h3></div>
      <span className={`review-pill ${affordable ? 'approved' : 'review'}`}>{incomplete ? 'Affordability unconfirmed' : revisionState === 'failed' ? 'Revision failed'
        : affordable ? 'Within budget' : reviewedOverBudget ? 'Over budget' : 'Budget review needed'}</span></div>
    {!trip.budget_revision_summary?.attempts && <p>{incomplete ? 'A complete cost estimate is required before affordability can be confirmed.'
      : affordable ? 'The complete planning estimate is within your original budget. This is separate from reviewer approval and booking readiness.'
      : 'The estimate requires budget review; supported alternatives or user-approved constraint changes may be needed.'}</p>}
    <div className="optimization-facts">
      <div><strong>Review</strong><span>{review ? review.review_status === 'APPROVED' && (incomplete || !isApproved(trip))
        ? 'Approval not confirmed' : humanize(review.review_status) : 'No reviewer result supplied'}</span></div>
      <div><strong>Revisions</strong><span>{trip.budget_revision_summary?.history.length
        ? `${trip.budget_revision_summary.history.length} interactive revision attempt(s) recorded; see the before-and-after results.`
        : revisionWarnings.length ? 'Revision attempt recorded by the planner' : 'Revision history was not supplied; optimization attempts cannot be confirmed.'}</span></div>
      <div><strong>Booking readiness</strong><span>Unverified</span></div>
    </div>
    {revisionWarnings.length > 0 && <details className="result-details"><summary>View optimization details</summary>
      <ul aria-label="Returned revision results">{revisionWarnings.map((warning) => <li key={warning}>{warning}</li>)}</ul></details>}
    {showSavings && <p className="supported-savings">Supported potential savings: <strong>{formatMoney(savings, trip.request.currency)}</strong>. This is a recommendation, not a confirmed reduction from a previous plan.</p>}
  </section>
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
    || getValue(meta, 'cost_classification') === 'unknown'
  const activityPrice = moneyValue(activity.estimated_cost)
  const activityCurrency = typeof meta.cost_currency === 'string' ? meta.cost_currency : currency
  const activityType = activity.category || (typeof meta.activity_type === 'string' ? meta.activity_type : null)
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
          {activityPrice != null && activityPrice >= 0 && !unknownCost && (
            <span>{formatMoney(activity.estimated_cost, activityCurrency)}</span>
          )}
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
        {activityType && (
          <span className={`activity-category ${activityType === 'meal_break' ? 'meal-category' : ''}`}>{humanize(activityType)}</span>
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
          <p className="activity-meta">Opening hours (verification not supplied): {openHours}</p>
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
  const warnings = [...new Set([...(trip?.errors ?? []), ...(trip?.warnings ?? []), ...(trip?.budget?.warnings ?? [])])]
  if (!trip || !warnings.length) return null
  const budget = trip.budget
  const overrun = moneyValue(budget?.over_budget_amount)
  const missing = trip.status === 'insufficient_data' || budget?.pricing_status === 'INCOMPLETE'
    || budget?.status === 'INCOMPLETE_ESTIMATE' || !!budget?.unpriced_components?.length
  const important = missing ? 'Some required costs or trip details are missing. This plan is incomplete.'
    : overrun != null && overrun > 0 && !budgetHasConflictingFigures(trip) && budget?.currency === trip.request.currency
      ? `This plan is over budget by ${formatMoney(overrun, trip.request.currency)}.`
    : trip.status === 'failed' ? 'Planning encountered an error. Review the details before retrying.'
    : trip.optimization_review?.issues?.some((issue) => issue.blocks_approval)
      ? 'Some required trip details still need confirmation.'
    : warnings.some((warning) => /weather|forecast/i.test(warning)) ? 'Check the weather before outdoor activities.'
    : null
  return <section className="warnings-card compact-result-card" aria-label="Trip warnings">
    <h3>Worth knowing</h3>
    <div aria-label="Important travel notes">
      {important && <p>{important}</p>}
      <p>Booking prices and availability are unverified.</p>
    </div>
    <details className="result-details">
      <summary>View details</summary>
      <ul>{warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
    </details>
  </section>
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

function shortMessage(value: string): string {
  const text = value.trim().replace(/\s+/g, ' ')
  const firstSentence = text.match(/^.*?[.!?](?:\s|$)/)?.[0].trim() ?? text
  return firstSentence.length <= 160 ? firstSentence : `${firstSentence.slice(0, 157).trimEnd()}…`
}

function ReviewNotes({ trip, approved }: { trip: TripDetails; approved: boolean }) {
  const review = trip.optimization_review
  if (!review) return null
  const issues = Array.isArray(review.issues) ? review.issues : []
  const warnings = Array.isArray(review.warnings) ? [...new Set(review.warnings)] : []
  const suggestions = Array.isArray(review.suggestions)
    ? review.suggestions
    : []
  const recommendation = issues.find((issue) => issue.blocks_approval && issue.recommended_action)?.recommended_action
    || suggestions.find((suggestion) => typeof suggestion.recommendation === 'string')?.recommendation
    || review.qualitative_summary
  const overrun = moneyValue(trip.budget?.over_budget_amount)
  return (
    <section className="review-notes compact-result-card" aria-label="Planner review">
      <h3>Planner review</h3>
      <p>{approved ? 'Reviewer approval confirmed. Booking readiness remains unverified.' : 'Reviewer approval is not confirmed.'}</p>
      {overrun != null && overrun > 0 && !budgetHasConflictingFigures(trip) && trip.budget?.currency === trip.request.currency
        && <p className="compact-overrun">Over budget: <strong>{formatMoney(overrun, trip.request.currency)}</strong></p>}
      {recommendation && <p aria-label="Review recommendation">{shortMessage(recommendation)}</p>}
      <details className="result-details">
        <summary>View details</summary>
      {typeof review.review_status === 'string' && (
        <p className="review-status-text">
          Status: {review.review_status === 'APPROVED' && !approved ? 'Approval not confirmed' : humanize(review.review_status)}
        </p>
      )}
      {review.qualitative_summary && <p aria-label="Reviewer summary">{review.qualitative_summary}</p>}
      {!!warnings.length && <div aria-label="Reviewer warnings">
        <strong>Reviewer warnings</strong>
        <ul>{warnings.map((warning, index) => <li key={`review-warning-${index}`}>{warning}</li>)}</ul>
      </div>}
      {!!issues.length && (
        <ul>
          {issues.map((issue, index) => (
            <li key={index}>
              {isRecord(issue) && typeof issue.description === 'string'
                ? issue.description
                : 'A planning issue was reported.'}
              {issue.blocks_approval === true && <p>Blocks approval</p>}
              {issue.evidence_basis && <p>Evidence: {issue.evidence_basis}</p>}
              {issue.recommended_action && <p>Recommended action: {issue.recommended_action}</p>}
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
      {!issues.length && !suggestions.length && !warnings.length && !review.qualitative_summary && (
        <p>No additional review notes were returned.</p>
      )}
      </details>
    </section>
  )
}

function applicableFlightReference(value: unknown, trip: TripDetails): boolean {
  if (!isRecord(value) || value.provider !== 'travelpayouts' || value.classification !== 'cached_reference'
    || value.applicability !== 'applicable' || !isRecord(value.requested_scope) || !isRecord(value.passenger_basis)) return false
  const scope = value.requested_scope
  const basis = value.passenger_basis
  const request = trip.request
  if (value.origin_iata !== scope.origin_iata || value.destination_iata !== scope.destination_iata
    || value.departure_date !== request.start_date || value.return_date !== request.end_date
    || scope.departure_date !== request.start_date || scope.return_date !== request.end_date
    || value.currency !== request.currency || scope.currency !== request.currency || scope.travelers !== request.travelers_count
    || typeof scope.origin_location !== 'string' || typeof scope.destination_location !== 'string'
    || scope.origin_location.trim().toLowerCase() !== request.origin.trim().toLowerCase()
    || scope.destination_location.trim().toLowerCase() !== request.destination.trim().toLowerCase()
    || typeof value.price !== 'string' || !Number.isFinite(Number(value.price)) || Number(value.price) <= 0) return false
  if (typeof basis.evidence !== 'string' || !basis.evidence.trim()
    || !((basis.kind === 'per_adult' && basis.priced_passengers === 1)
      || (basis.kind === 'party_total' && basis.priced_passengers === request.travelers_count))) return false
  const now = Date.now()
  const retrieved = typeof value.retrieved_at === 'string' ? Date.parse(value.retrieved_at) : NaN
  if (!Number.isFinite(retrieved) || retrieved > now || now - retrieved > 48 * 60 * 60 * 1000) return false
  if (value.expires_at != null && (typeof value.expires_at !== 'string'
    || !Number.isFinite(Date.parse(value.expires_at)) || Date.parse(value.expires_at) <= now)) return false
  if (value.found_at != null && (typeof value.found_at !== 'string' || !Number.isFinite(Date.parse(value.found_at))
    || Date.parse(value.found_at) > retrieved || now - Date.parse(value.found_at) > 48 * 60 * 60 * 1000)) return false
  return true
}

function hasRejectedFlightReference(item: unknown, trip: TripDetails): boolean {
  if (!isRecord(item)) return false
  if (item.flight_reference != null) return !applicableFlightReference(item.flight_reference, trip)
    || item.source !== 'reference_data' || item.classification !== 'estimated' || item.evidence_kind === 'verified_price'
  return getArray(item, 'source_references').some((source) => getValue(source, 'provider') === 'travelpayouts')
}

function isAIFlightAssumption(item: BudgetLineItem, trip: TripDetails): boolean {
  const ai = item.ai_provenance
  const rate = ai?.unit_rate
  const scope = ai?.scope
  const amount = moneyValue(item.amount)
  const unitRate = moneyValue(rate?.typical)
  return item.category === 'intercity_transport' && amount != null && amount > 0
    && item.source === 'configured_assumption' && item.classification === 'estimated'
    && item.evidence_kind === 'planning_estimate' && item.confidence === 'low'
    && ai?.method === 'gemini_planning_assumption' && ai.confidence === 'low'
    && ai.live_availability_checked === false && !!ai.model?.trim()
    && Number.isFinite(Date.parse(ai.generated_at))
    && rate?.category === 'intercity_transport' && rate.unit === 'traveler_round_trip'
    && unitRate != null && unitRate > 0 && ai.quantity === trip.request.travelers_count
    && Math.round(unitRate * ai.quantity * 100) === Math.round(amount * 100)
    && item.currency === trip.request.currency && rate.currency === item.currency
    && scope?.currency === item.currency && scope.transport_mode === 'flight'
    && scope.origin === trip.request.origin && scope.destination === trip.request.destination
    && scope.start_date === trip.request.start_date && scope.end_date === trip.request.end_date
    && scope.travelers_count === trip.request.travelers_count
    && scope.travel_style === trip.request.travel_style
    && !item.flight_reference && !item.source_references?.length && !item.price_sources?.length
    && !item.currency_conversion && !item.optional
}

function FlightPricingEvidence({ trip }: { trip: TripDetails }) {
  const items = trip.budget?.line_items ?? []
  const mode = trip.request.cost_preferences?.transport_mode ?? trip.request.constraints?.intercity_routing_mode
  const references = trip.budget?.flight_references ?? []
  const flights = items.filter((item) => item.category === 'intercity_transport' || item.flight_reference != null)
  if (mode != null && mode !== 'flight' && !references.length && !flights.some((item) => item.flight_reference != null)) return null
  const selected = flights.filter((item) => moneyValue(item.amount) != null && Number(item.amount) >= 0
    && item.classification !== 'unknown' && item.currency === trip.request.currency && !hasRejectedFlightReference(item, trip))
  const excluded = references.filter((reference) => !applicableFlightReference(reference, trip))
  const rejectedAttached = flights.flatMap((item) => item.flight_reference && !applicableFlightReference(item.flight_reference, trip)
    ? [item.flight_reference] : [])
  const rejected = [...new Map([...excluded, ...rejectedAttached].map((reference) => [
    `${reference.origin_iata}/${reference.destination_iata}/${reference.departure_date}/${reference.return_date}/${reference.retrieved_at}`,
    reference,
  ])).values()]
  const verified = selected.length > 0 && selected.every(isVerifiedProviderPrice)
  return <section className="day-card compact-result-card flight-cost-card" aria-label="Flight Cost Estimate">
    <h3>Flight Cost Estimate</h3>
    {!selected.length && <p className="inline-note">No applicable flight price is included. Flight pricing remains unavailable.</p>}
    {selected.map((item, index) => <div className="flight-cost-summary" key={`flight-summary-${index}`}>
      <strong>{formatMoney(item.amount, item.currency!)}</strong>
      <span className="review-pill">{isAIFlightAssumption(item, trip) ? 'AI Estimate'
        : isVerifiedProviderPrice(item) ? 'Verified provider price'
        : item.source === 'user_assumption' ? 'Your allowance'
        : item.source === 'reference_data' || item.flight_reference ? 'Cached / reference estimate' : 'Planning estimate'}</span>
    </div>)}
    <p className="inline-note">{verified ? 'Provider-verified price. Booking availability must be confirmed.'
      : 'Not a verified airline fare. Actual prices may vary.'}</p>
    {(selected.length > 0 || rejected.length > 0) && <details className="result-details">
      <summary>View pricing details</summary>
    {selected.map((item, index) => {
      const reference = item.flight_reference
      const providers = [...new Set([...(item.source_references ?? []).map((source) => source.provider), reference?.provider].filter(Boolean))]
      return <div key={`flight-evidence-${index}`}>
        <p>{costSourceLabel(item)}</p>
        {providers.length > 0 && <p>Provider: {providers.map((provider) => humanize(String(provider))).join(', ')}</p>}
        {item.source && <p>Source: {humanize(item.source)}</p>}
        {item.evidence_kind && <p>Evidence: {humanize(item.evidence_kind)}</p>}
        {item.confidence && !isAIFlightAssumption(item, trip) && <p>Confidence: {humanize(item.confidence)}</p>}
        {item.currency && <p>Currency: {item.currency}</p>}
        {isAIFlightAssumption(item, trip) && <CostEvidence item={item} />}
        {reference ? <>
          <p>Flight classification: {humanize(reference.classification)}</p>
          <p>Route: {reference.origin_iata} → {reference.destination_iata}</p>
          <p>Departure: {reference.departure_date} · Return: {reference.return_date}</p>
          <p>Retrieved: {reference.retrieved_at}</p>
          {reference.found_at && <p>Observed: {reference.found_at}</p>}
          {reference.expires_at && <p>Expires: {reference.expires_at}</p>}
          {reference.passenger_basis && <>
            <p>Passenger pricing basis: {humanize(reference.passenger_basis.kind)}
              {reference.passenger_basis.priced_passengers != null && ` · ${reference.passenger_basis.priced_passengers} priced passenger(s)`}</p>
            {reference.passenger_basis.evidence && <p>Basis evidence: {reference.passenger_basis.evidence}</p>}
          </>}
          <p className="inline-note">Cached search-history estimate. Not live availability or a confirmed booking price.</p>
        </> : <p className="inline-note">
          {isAIFlightAssumption(item, trip) ? 'AI-generated planning assumption; booking readiness remains unverified.'
            : item.source === 'user_assumption'
            ? 'A user-provided flight allowance is included; no provider fare reference was returned.'
            : 'No structured flight reference was returned. The included price uses the source and evidence shown above.'}
        </p>}
        {!reference && item.price_sources?.map((source, sourceIndex) => <p key={`flight-source-${sourceIndex}`}>
          {source.name}{source.observed_on && ` · Observed: ${source.observed_on}`}
        </p>)}
      </div>
    })}
    {!!rejected.length && <div className="unpriced-list" aria-label="Excluded flight references">
      <strong>References not included in budget costs</strong>
      <ul>{rejected.map((reference, index) => <li key={`excluded-flight-${index}`}>
        {reference.origin_iata} → {reference.destination_iata}
        {reference.departure_date && ` · Departure: ${reference.departure_date}`}
        {reference.return_date && ` · Return: ${reference.return_date}`}
        {' · '}{reference.applicability && reference.applicability !== 'applicable'
          ? humanize(reference.applicability) : 'Does not apply to this trip or is no longer current'}.
      </li>)}</ul>
    </div>}
    </details>}
  </section>
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
  const total = trip.budget?.estimated_total !== undefined ? trip.budget.estimated_total : trip.total_estimated_cost
  return (
    trip.status === 'completed' &&
    !budgetHasConflictingFigures(trip) &&
    moneyValue(total) != null && Number(total) >= 0 && Number(total) <= Number(trip.request.total_budget) &&
    trip.currency === trip.request.currency &&
    (trip.budget?.currency == null || trip.budget.currency === trip.request.currency) &&
    !['OVER_BUDGET', 'INCOMPLETE_ESTIMATE', 'INVALID_INPUT'].includes(trip.budget?.status ?? '') &&
    trip.budget?.pricing_status !== 'INCOMPLETE' &&
    !trip.budget?.unpriced_components?.length &&
    !trip.budget?.category_totals?.some((category) => category.included !== false && category.complete === false) &&
    !trip.budget?.line_items?.some((item) => moneyValue(item.amount) == null || Number(item.amount) < 0 || item.currency !== trip.request.currency) &&
    trip.optimization_review?.approved === true &&
    trip.optimization_review.review_status === 'APPROVED' &&
    !trip.optimization_review.issues?.some((issue) => issue.blocks_approval === true) &&
    !trip.budget?.line_items?.some((item) => hasRejectedFlightReference(item, trip))
  )
}

function budgetHasConflictingFigures(trip: TripDetails): boolean {
  const budget = trip.budget
  if (!budget) return false
  const total = moneyValue(budget.estimated_total)
  const subtotal = moneyValue(budget.known_cost_subtotal)
  const original = moneyValue(trip.request.total_budget)
  const cents = (value: number) => Math.round(value * 100)
  if (budget.total_budget != null && moneyValue(budget.total_budget) !== original) return true
  if (budget.known_cost_subtotal != null && (subtotal == null || subtotal < 0)) return true
  if (total != null && subtotal != null && cents(total) !== cents(subtotal)) return true
  if (total != null && original != null && (
    (budget.remaining_budget != null && (moneyValue(budget.remaining_budget) == null || cents(Number(budget.remaining_budget)) !== cents(original) - cents(total)))
    || (budget.over_budget_amount != null && (moneyValue(budget.over_budget_amount) == null || cents(Number(budget.over_budget_amount)) !== Math.max(0, cents(total) - cents(original))))
  )) return true
  // Reconcile a fully described ledger. Older partial metadata is left intact;
  // never infer a replacement total from category or line-item amounts.
  const categories = budget.category_totals?.filter((category) => category.included !== false) ?? []
  const lines = budget.line_items ?? []
  if (categories.length && lines.length && categories.every((category) => typeof category.line_item_count === 'number')
    && categories.reduce((count, category) => count + category.line_item_count!, 0) === lines.length
    && lines.every((line) => moneyValue(line.amount) != null && line.currency === trip.request.currency)
    && subtotal != null && lines.reduce((sum, line) => sum + cents(Number(line.amount)), 0) !== cents(subtotal)) return true
  return false
}

function isVerifiedProviderPrice(item: unknown): boolean {
  if (!isRecord(item) || typeof item.amount !== 'string' || item.amount.trim() === '' || !Number.isFinite(Number(item.amount)) || Number(item.amount) < 0) return false
  const sources = getArray(item, 'source_references')
  if (item.ai_provenance != null || item.flight_reference != null || sources.some((source) => getValue(source, 'provider') === 'travelpayouts')) return false
  return item.evidence_kind === 'verified_price' && item.classification === 'verified'
    && item.source === 'verified_provider' && sources.some((source) => isRecord(source)
      && typeof source.title === 'string' && source.title.trim().length > 0
      && typeof source.url === 'string' && isSafeUrl(source.url))
}

function pricingIsVerified(budget: Record<string, unknown> | null, lineItems: unknown[]): boolean {
  return budget?.pricing_status === 'LIVE_PRICE_VERIFIED' && lineItems.length > 0
    && lineItems.every(isVerifiedProviderPrice)
}

function costSourceLabel(item: Record<string, unknown>): string {
  if (moneyValue(item.amount) == null || Number(item.amount) < 0 || item.classification === 'unknown') return 'Unknown · Missing price'
  if (item.ai_provenance != null) return 'AI-generated planning estimate · low confidence'
  if (item.source === 'user_assumption') return 'User-provided planning estimate · user-provided allowance'
  if (item.source === 'reference_data' || item.flight_reference != null
    || getArray(item, 'source_references').some((source) => getValue(source, 'provider') === 'travelpayouts')) return 'Reference-based planning estimate · not a live quote'
  if (isVerifiedProviderPrice(item)) return 'Verified provider price'
  return 'Non-live planning estimate'
}

function CostEvidence({ item }: { item: Record<string, unknown> }) {
  const sources = getArray(item, 'price_sources')
  const assumptions = getArray(item, 'assumptions')
  const conversion = getValue(item, 'currency_conversion')
  const ai = getValue(item, 'ai_provenance')
  const rate = getValue(ai, 'unit_rate')
  return <>
    {isRecord(ai) && isRecord(rate) && <small className="ai-unit-rate">
      AI assumption: {formatMoney(rate.typical, String(rate.currency ?? item.currency))} {unitLabel(rate.unit)}
      {typeof ai.quantity === 'number' && ` × ${ai.quantity} units`}. Low confidence; no live availability checked.
    </small>}
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
  const amount = moneyValue(value)
  if (amount == null || amount < 0) return 'Not available'
  try {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency,
      currencyDisplay: 'code',
      maximumFractionDigits: 2,
    }).format(amount)
  } catch {
    return `${currency} ${amount.toFixed(2)}`
  }
}

function moneyValue(value: unknown): number | null {
  if ((typeof value !== 'string' && typeof value !== 'number') || String(value).trim() === '') return null
  const amount = Number(value)
  return Number.isFinite(amount) ? amount : null
}

function formatDate(value: string): string {
  const date = new Date(`${value}T00:00:00Z`)
  return Number.isFinite(date.getTime())
    ? new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' }).format(date)
    : value
}

function tripDuration(start: string, end: string): string {
  const days = (Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / 86_400_000 + 1
  return Number.isInteger(days) && days > 0 ? `${days} ${days === 1 ? 'day' : 'days'}` : 'Not available'
}

function unitLabel(unit: unknown): string {
  const labels: Record<string, string> = {
    traveler_round_trip: 'per traveler, round trip', room_night: 'per room per night',
    traveler_day: 'per traveler per day', party_day: 'per party per day', traveler_trip: 'per traveler per trip',
  }
  return typeof unit === 'string' ? labels[unit] ?? humanize(unit) : '(unit not supplied)'
}

function humanize(value: string): string {
  return value
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (character) => character.toUpperCase())
}
