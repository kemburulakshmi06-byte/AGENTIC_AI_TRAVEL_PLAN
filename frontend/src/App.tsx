import { useEffect, useState } from 'react'
import { getSavedTrips } from './api'
import { PlannerForm } from './components/PlannerForm'
import { TripWorkspace } from './components/TripWorkspace'
import type { TripCreatedResponse, TripListResponse } from './types'

const CURRENT_YEAR = new Date().getFullYear()
const ACTIVE_TRIP_KEY = 'wayfare.activeTripId'

function restoredTrip(): TripCreatedResponse | null {
  try {
    const id = sessionStorage.getItem(ACTIVE_TRIP_KEY)
    if (id && /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(id)) {
      return { trip_id: id, task_id: id, status: 'queued', message: 'Restoring saved trip.' }
    }
  } catch { /* Storage may be disabled; saved trips remain accessible below. */ }
  return null
}

function App() {
  const [activeTrip, setActiveTrip] = useState<TripCreatedResponse | null>(restoredTrip)
  const [savedTrips, setSavedTrips] = useState<TripListResponse | null>(null)
  const [savedTripsMessage, setSavedTripsMessage] = useState(
    'Checking trip-list access…',
  )

  useEffect(() => {
    const controller = new AbortController()
    getSavedTrips(controller.signal)
      .then((result) => {
        setSavedTrips(result)
        setSavedTripsMessage('')
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return
        setSavedTripsMessage(
          error instanceof Error && 'status' in error && error.status === 404
            ? 'Saved trips are private in this environment. Trip listing is currently disabled.'
            : 'Saved trips are temporarily unavailable.',
        )
      })
    return () => controller.abort()
  }, [])

  function startOver() {
    setActiveTrip(null)
    try { sessionStorage.removeItem(ACTIVE_TRIP_KEY) } catch { /* Optional browser storage. */ }
    document.getElementById('planner')?.scrollIntoView({ behavior: 'smooth' })
  }

  function selectTrip(trip: TripCreatedResponse) {
    setActiveTrip(trip)
    try { sessionStorage.setItem(ACTIVE_TRIP_KEY, trip.trip_id) } catch { /* Optional browser storage. */ }
  }

  return (
    <div className="site-shell">
      <header className="site-header">
        <a className="brand" href="#top" aria-label="Wayfare home">
          <span className="brand-mark" aria-hidden="true">
            <svg viewBox="0 0 36 36" fill="none">
              <path
                d="M18 3.5 21.5 14l10.5 4-10.5 3.5L18 32l-3.5-10.5L4 18l10.5-4L18 3.5Z"
                fill="currentColor"
              />
              <circle cx="28.5" cy="8" r="2.5" fill="#36C6B1" />
            </svg>
          </span>
          <span>wayfare<span className="brand-period">.</span></span>
        </a>
        <nav className="main-nav" aria-label="Main navigation">
          <a href="#how-it-works">How it works</a>
          <a href="#features">Why Wayfare</a>
          <a href="#saved-trips">My trips</a>
        </nav>
        <a className="header-cta" href="#planner">
          Plan your trip <span aria-hidden="true">↗</span>
        </a>
      </header>

      <main id="top">
        <section className="hero">
          <div className="hero-content">
            <div className="hero-kicker">
              <span className="live-dot" /> AGENTIC AI TRAVEL PLANNER
            </div>
            <h1>
              The world is wide.
              <br />
              <span>Your plans can be simple.</span>
            </h1>
            <p className="hero-copy">
              Tell us what moves you. We’ll turn it into a thoughtful,
              well-paced trip that makes the most of your time and budget.
            </p>
            <div className="hero-actions">
              <a className="button button-primary" href="#planner">
                Start planning <span aria-hidden="true">↗</span>
              </a>
              <a className="text-link" href="#how-it-works">
                See how it works <span aria-hidden="true">↓</span>
              </a>
            </div>
            <div className="hero-assurance">
              <span className="avatar-stack" aria-hidden="true">
                <i>✦</i><i>↗</i><i>⌖</i>
              </span>
              <span>Made for curious minds and open calendars.</span>
            </div>
          </div>
          <div className="hero-art" role="img" aria-label="Mountain lake travel landscape">
            <div className="hero-art-overlay" />
            <div className="floating-note">
              <span className="note-icon" aria-hidden="true">✧</span>
              <div>
                <span>YOUR NEXT ADVENTURE</span>
                <strong>Somewhere wonderful.</strong>
              </div>
            </div>
            <span className="image-credit">Find your elsewhere</span>
          </div>
          <div className="hero-bottom">
            <div><strong>One thoughtful plan</strong><span>Not a dozen browser tabs</span></div>
            <span className="hero-bottom-divider" />
            <div><strong>Built around you</strong><span>Your pace, your priorities</span></div>
            <span className="hero-bottom-divider" />
            <div><strong>Budget-aware</strong><span>More clarity, fewer surprises</span></div>
          </div>
        </section>

        <section className="planner-section section-wrap" id="planner">
          {activeTrip ? (
            <>
              <div className="section-intro">
                <span className="eyebrow">THE JOURNEY STARTS HERE</span>
                <h2>We’re on it.</h2>
                <p>Follow along as your personalized trip comes together.</p>
              </div>
              <TripWorkspace
                key={activeTrip.trip_id}
                tripId={activeTrip.trip_id}
                initialStatus={activeTrip.status}
                onStartOver={startOver}
              />
            </>
          ) : (
            <div className="planner-layout">
              <div className="planner-intro">
                <span className="eyebrow">A BETTER WAY TO GET AWAY</span>
                <h2>Less planning.<br /><span>More anticipation.</span></h2>
                <p>
                  A team of AI travel specialists researches your destination,
                  balances the budget, and brings your days together.
                </p>
                <div className="planner-proof">
                  <span className="proof-icon" aria-hidden="true">◎</span>
                  <div>
                    <strong>Your budget stays yours</strong>
                    <p>We never quietly stretch your spending limit.</p>
                  </div>
                </div>
                <div className="planner-proof">
                  <span className="proof-icon proof-teal" aria-hidden="true">↗</span>
                  <div>
                    <strong>Thoughtful, not overstuffed</strong>
                    <p>Plans that leave room to enjoy the moment.</p>
                  </div>
                </div>
              </div>
              <PlannerForm onTripCreated={selectTrip} />
            </div>
          )}
        </section>

        <section className="how-section" id="how-it-works">
          <div className="section-wrap">
            <div className="section-intro centered">
              <span className="eyebrow">A LITTLE LESS WORK, A LOT MORE WONDER</span>
              <h2>From “what if” to “let’s go.”</h2>
              <p>Good trips come together one considered step at a time.</p>
            </div>
            <div className="steps-grid">
              <article className="step-card">
                <span className="step-number">01</span>
                <span className="step-symbol" aria-hidden="true">⌖</span>
                <h3>Tell us what you love</h3>
                <p>Start with where you’re going, when, and what makes a trip yours.</p>
              </article>
              <article className="step-card">
                <span className="step-number">02</span>
                <span className="step-symbol teal" aria-hidden="true">✧</span>
                <h3>Let the team explore</h3>
                <p>Specialist agents research, plan, and check the details together.</p>
              </article>
              <article className="step-card">
                <span className="step-number">03</span>
                <span className="step-symbol blue" aria-hidden="true">↗</span>
                <h3>Make it your own</h3>
                <p>Get a day-by-day starting point, with costs and caveats in view.</p>
              </article>
            </div>
          </div>
        </section>

        <section className="features-section section-wrap" id="features">
          <div className="feature-copy">
            <span className="eyebrow">TRAVEL, WITH A LITTLE MORE THOUGHT</span>
            <h2>Big-picture wonder.<br /><span>Down-to-earth details.</span></h2>
            <p>
              Wayfare brings the moving pieces together without pretending
              travel is perfectly predictable. See what’s known, what’s
              estimated, and what still needs a closer look.
            </p>
            <a className="text-link" href="#planner">
              Find your way <span aria-hidden="true">↗</span>
            </a>
          </div>
          <div className="feature-stack">
            <article className="feature-card">
              <span className="feature-icon">✦</span>
              <div><h3>Plans shaped around you</h3><p>Interests, travel style, and your own notes all have a place.</p></div>
              <span className="feature-arrow">↗</span>
            </article>
            <article className="feature-card">
              <span className="feature-icon mint">◷</span>
              <div><h3>A pace that feels human</h3><p>Days designed to balance discovery with a little breathing room.</p></div>
              <span className="feature-arrow">↗</span>
            </article>
            <article className="feature-card">
              <span className="feature-icon pale">◎</span>
              <div><h3>Clarity where it counts</h3><p>Budget notes and planning caveats, not made-up certainty.</p></div>
              <span className="feature-arrow">↗</span>
            </article>
          </div>
        </section>

        <section className="saved-section" id="saved-trips">
          <div className="section-wrap saved-inner">
            <div>
              <span className="eyebrow">YOUR TRAVEL JOURNAL</span>
              <h2>Trips you’ve started.</h2>
            </div>
            {savedTrips ? (
              savedTrips.items.length ? (
                <div className="saved-trip-list">
                  {savedTrips.items.map((trip) => (
                    <article className="saved-trip" key={trip.id}>
                      <span className="saved-trip-icon" aria-hidden="true">↗</span>
                      <div><strong>{trip.title}</strong><span>{trip.origin} → {trip.destination} · {trip.start_date}</span></div>
                      <span className="saved-status">{trip.status.replaceAll('_', ' ')}</span>
                      <button className="text-link" onClick={() => {
                        selectTrip({ trip_id: trip.id, task_id: trip.id, status: trip.status, message: 'Loading saved trip.' })
                        document.getElementById('planner')?.scrollIntoView({ behavior: 'smooth' })
                      }}>View trip</button>
                    </article>
                  ))}
                </div>
              ) : (
                <p className="saved-note">No saved trips to show just yet.</p>
              )
            ) : (
              <p className="saved-note" role="status">
                <span className="saved-lock" aria-hidden="true">⌑</span>
                {savedTripsMessage}
              </p>
            )}
          </div>
        </section>
      </main>

      <footer className="site-footer">
        <a className="brand footer-brand" href="#top">
          <span className="brand-mark" aria-hidden="true">✦</span>
          <span>wayfare<span className="brand-period">.</span></span>
        </a>
        <p>Go thoughtfully. Come back with stories.</p>
        <span>© {CURRENT_YEAR} Wayfare · A travel planning experience</span>
      </footer>
    </div>
  )
}

export default App
