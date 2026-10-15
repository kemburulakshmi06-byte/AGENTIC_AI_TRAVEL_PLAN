# Agentic AI Travel Planning & Itinerary Optimization System

Phases 1 through 5 establish the monorepo, Python/FastAPI backend foundation and configuration, PostgreSQL persistence models and migration, Upstash Redis REST caching and operational task status, and asynchronous travel information API tools. Phase 6 Steps 1 through 7 provide Gemini generation, shared agent contracts, Requirements, Destination Research, Accommodation & Transport, Budget, Itinerary, and Optimization & Review agents. Phase 7 adds async LangGraph orchestration. Phase 8 adds trip planning APIs and PostgreSQL persistence. Phase 9 provides the React trip-planning experience; deployment remains future work.

## Architecture

```text
.
├── backend/
│   ├── app/
│   │   ├── agents/       # Six specialized travel-planning agents
│   │   ├── api/          # FastAPI routes
│   │   ├── core/         # Application configuration and shared concerns
│   │   ├── database/     # SQLAlchemy models and session management
│   │   ├── workflows/    # Async LangGraph trip-planning workflow
│   │   ├── schemas/      # Pydantic request, response, and agent schemas
│   │   ├── services/     # Persistence, Redis REST, cache and task status
│   │   ├── tools/        # Async Geoapify, ORS, Open-Meteo and Tavily clients
│   │   └── main.py       # FastAPI application entry point
│   ├── tests/
│   └── requirements.txt
└── frontend/
    └── src/              # React + TypeScript trip-planning experience
```

The backend stack includes FastAPI, Pydantic v2, LangGraph, SQLAlchemy 2.x, Alembic, Psycopg 3, Neon PostgreSQL, Upstash Redis REST, the Google Gen AI SDK, and async httpx integrations for Geoapify, OpenRouteService, Open-Meteo, and Tavily. The frontend uses React, TypeScript, Vite, Tailwind CSS, and React Router. Deployment is planned for Render (backend) and Vercel (frontend).

## Prerequisites

- Python 3.12
- Node.js 20.19+ or 22.12+ (required by the Vite 8 toolchain)
- npm

## Local setup

### Backend (PowerShell)

```powershell
cd backend
..\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
uvicorn app.main:app --reload --port 8001
```

If Vite runs on port 5175, set `FRONTEND_URL=http://localhost:5175` in
`backend/.env` and add `http://localhost:5175` and `http://127.0.0.1:5175`
to its JSON `CORS_ORIGINS` list, retaining any other required origins.
CORS origins include the frontend port; the backend port 8001 belongs in
the frontend's `VITE_API_BASE_URL`. Restart Uvicorn after changing
`backend/.env` because settings are cached and `.env` changes may not trigger reload.

Settings are loaded from `backend/.env` regardless of whether Uvicorn starts from the repository root or `backend/`. Configure `APP_NAME`, `APP_ENV` (`development`, `testing`, or `production`), `APP_DEBUG`, `API_V1_PREFIX`, `FRONTEND_URL`, `CORS_ORIGINS`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_TIMEOUT_SECONDS`, `GEMINI_MAX_RETRIES`, `GEOAPIFY_API_KEY`, `OPENROUTESERVICE_API_KEY`, `TAVILY_API_KEY`, `AMADEUS_API_KEY`, `AMADEUS_API_SECRET`, `AMADEUS_ENVIRONMENT`, `AUTOMATIC_COST_ESTIMATION_ENABLED`, `COST_REFERENCE_FILE`, `DEFAULT_ESTIMATE_CONTINGENCY_PERCENT`, `REFERENCE_FX_ENABLED`, `REFERENCE_FX_MAX_AGE_DAYS`, `DATABASE_URL`, `TEST_DATABASE_URL`, `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN`, `EXTERNAL_API_TIMEOUT_SECONDS`, `TRIP_EXECUTION_MODE`, `TRIP_WORKER_ENABLED`, `TRIP_JOB_LEASE_SECONDS`, `TRIP_JOB_MAX_ATTEMPTS`, and `TRIP_WORKER_POLL_SECONDS` as needed. `APP_DEBUG` avoids collisions with generic machine-level `DEBUG` variables. API keys and database settings are loaded from environment configuration; keep real values in the ignored `.env` file and never put secrets in source control.

### Google Gemini content generation

Phase 6 Step 1 provides `backend/app/services/gemini_service.py`, an asynchronous Google Gen AI SDK wrapper. It reads `GEMINI_API_KEY` and `GEMINI_MODEL` from settings, initializes the SDK client only on the first generation request, and supports both text output and Pydantic-validated JSON output through `generate_structured()` or `generate_content(response_model=...)`. Use `async with GeminiService() as gemini:` or call `await gemini.aclose()` to close the async SDK client.

`GEMINI_TIMEOUT_SECONDS` defaults to `30` and configures both the SDK HTTP timeout and an overall request timeout. `GEMINI_MAX_RETRIES` defaults to `2` (zero to five retries) and retries timeouts, transport failures, HTTP 429, and server errors with a bounded delay. Authentication failures, unavailable models, rate limits after retry exhaustion, API failures, and invalid/model-schema responses raise sanitized typed errors. The service does not log credentials or raw provider error bodies. Mocked Gemini tests make no network requests; live verification is an explicit, separately invoked operation.

### Phase 6, Step 2: Requirements Agent

`backend/app/agents/requirements_agent.py` validates and normalizes structured travel requests, reports missing required values and detectable budget/interest conflicts, and calculates inclusive calendar-day duration when both dates are known. `TravelRequest`, `AgentContext`, generic `AgentResult`, and `NormalizedTravelRequirements` are shared typed contracts in `backend/app/schemas/agent_schemas.py`. Origins, destinations, dates, travelers, budget, and currency remain unset when not supplied; deterministic validation does not require Gemini credentials.

Gemini is called only when a natural-language user message is supplied. Its structured extraction is Pydantic-validated, and explicit structured values always take precedence over extracted values. User text is passed as untrusted data under a fixed extraction-only instruction. Gemini errors result in a warning and deterministic structured data is still returned; the agent does not invent missing values. Later Phase 6 steps add the remaining specialized agents, and Phase 7 wires them together with LangGraph.

### Phase 6, Step 3: Destination Research Agent

`backend/app/agents/destination_research_agent.py` consumes normalized requirements through `AgentContext.normalized_requirements` and returns a typed `DestinationResearch` output. It reuses Geoapify for geocoding and nearby attractions, Open-Meteo for supported travel-date forecasts, Tavily for separate attraction/history/food/travel-tip/seasonal searches, and the existing Gemini service for structured summaries, interest-aware ranking, and activity suggestions.

Geoapify place IDs and coordinates, Open-Meteo daily forecasts, and Tavily source URLs are provider facts. Gemini output cannot supply citations or alter provider coordinates/place IDs; unknown place IDs are discarded. Search snippets are marked untrusted and are never treated as instructions. Partial provider failures produce warnings while retaining available results. Open-Meteo data outside its forecast horizon is reported unavailable rather than fabricated. Existing provider tools retain their Redis cache-aside behavior.

Example (fictional, abbreviated):

```json
{
  "destination_name": "Sample City, Example Country",
  "latitude": 12.34,
  "longitude": 56.78,
  "attractions": [
    {
      "place_id": "provider-place-id",
      "name": "Example History Museum",
      "latitude": 12.35,
      "longitude": 56.79,
      "provider": "geoapify",
      "verified_provider_fact": true
    }
  ],
  "weather_summary": {
    "provider": "open-meteo",
    "forecast_available": false,
    "daily_forecasts": [],
    "limitation": "Dates outside forecast window"
  },
  "source_references": [
    {
      "provider": "tavily",
      "title": "Example destination guide",
      "url": "https://example.invalid/guide",
      "topic": "travel_tips",
      "content_is_untrusted": true
    }
  ],
  "warnings": []
}
```

Geocoding evaluates up to five provider candidates, checks the requested location,
and rejects ambiguous top matches. Broad country/region requests use provider
boundaries for city discovery or request clarification. Gemini suggestions are
interpretive rather than verified provider facts.

### Phase 6, Step 4: Accommodation & Transport Agent

`backend/app/agents/accommodation_transport_agent.py` consumes the shared
`AgentContext`, normalized travel requirements, and optional serialized
`DestinationResearch` output. It returns an `AccommodationTransportPlan`
through the shared `AgentResult` contract. The plan includes accommodation
category suggestions, estimated room occupancy and nights, transport options,
verified route data when available, component cost estimates, unpriced
components, preserved destination-research source references, assumptions, and
warnings.

Accommodation categories and transport preferences may be generated as
structured Gemini output; deterministic recommendations remain available
without Gemini credentials. Gemini cannot provide verified prices, routes,
coordinates, or provider IDs. Stay-area suggestions are retained only when
they exactly match destination research or an explicit user preference.

Origin coordinates are resolved with Geoapify, destination coordinates come
from destination research, and driving distance/duration are read from the
existing OpenRouteService tool. The tool's Redis route cache avoids redundant
provider requests. Missing or mismatched coordinates, unreachable routes, and
provider failures produce partial plans and warnings instead of fabricated
route values. OpenRouteService-supported routed modes are driving, walking,
and cycling; flight, train, bus, and local transit options do not include live
schedules or availability.

No hotel-booking provider is configured. The agent never returns named hotel
listings or claims live room availability. It uses the typed `cost_preferences`
fields supplied by the frontend for room rates, room count, daily local
transport, and round-trip fares per traveler. These take precedence over
legacy explicit values under `constraints.cost_assumptions` (`accommodation_nightly_rate_per_room`,
`local_transport_total`, and `intercity_transport_total`). Amounts based on
these user-supplied assumptions are marked `estimate`; missing pricing inputs
are `unknown` and are not silently counted as zero. Currency is not converted,
and costs stay unknown when currency is missing. The combined cost stays
unknown whenever any included component is unpriced. Explicit component
estimates are compared with the total trip budget when all required values are
available; an over-budget total is warned, not silently adjusted.

Example input fragment (fictional):

```json
{
  "normalized_requirements": {
    "origin": "Chennai",
    "destination": "Hyderabad",
    "start_date": "2027-01-10",
    "end_date": "2027-01-13",
    "travelers_count": 3,
    "currency": "INR",
    "travel_style": "budget",
    "constraints": {
      "cost_assumptions": {
        "accommodation_nightly_rate_per_room": "1800.00",
        "local_transport_total": "1200.00",
        "intercity_transport_total": "5000.00"
      }
    }
  }
}
```

The resulting accommodation estimate uses two rooms (assuming two travelers
per room) for three nights: `INR 10800.00`, explicitly labeled as an estimate,
not a hotel quote. A route distance/duration appears only if both endpoint
coordinates are verified and OpenRouteService returns a route.

### Phase 6, Step 5: Budget Agent

`backend/app/agents/budget_agent.py` consumes the shared `AgentContext` and
normalized requirements, plus the optional serialized
`accommodation_transport_plan`. It returns a `BudgetBreakdown` through the
shared `AgentResult` contract. The Pydantic schemas in
`backend/app/schemas/budget.py` describe budget line items, category subtotals,
validation status, and reduction suggestions.

All monetary values and calculations use `Decimal`. The agent uses the
accommodation, local-transport, and intercity-transport component costs from
the accommodation plan; it deliberately ignores that plan's combined total
to avoid counting the same expenses twice. Food and activity costs are
provided under `normalized_requirements.constraints.budget_assumptions` as
explicit totals or per-traveler-per-day amounts. Optional contingency can be
provided as a total amount or percentage, and other known costs are supplied
as a list. Required categories are accommodation, local transport, intercity
transport, food, and activities; use an explicit zero amount when a category
does not apply. Contingency is optional unless supplied. Per-day amounts are
multiplied by the traveler count and inclusive trip duration.

Example assumption input (fictional):

```json
{
  "budget_assumptions": {
    "food": {
      "per_traveler_per_day": "1000.00",
      "pricing_basis": "User planning assumption"
    },
    "activities": {
      "amount": "3000.00",
      "optional": true,
      "currency": "INR",
      "pricing_basis": "User planning assumption"
    },
    "contingency": {
      "percentage": "10"
    },
    "other_expenses": [
      {
        "description": "Museum tickets",
        "amount": "500.50",
        "currency": "INR",
        "pricing_basis": "User planning assumption"
      }
    ]
  }
}
```

For a fictional trip with a total budget of `INR 30,000.00`, accommodation of
`INR 10,000.00`, local transport of `INR 2,000.00`, intercity transport of
`INR 5,000.00`, three travelers for two days at `INR 1,000.00` per traveler
per day for food, `INR 3,000.00` activities, and `INR 500.50` other expenses,
the priced base is `INR 26,500.50`. A 10% contingency is `INR 2,650.05`,
making the estimated total `INR 29,150.55`, with `INR 849.45` remaining.
These are illustrative user-supplied estimates, not live quotes.

Statuses are deterministic: `OVER_BUDGET` means known priced costs already
exceed the budget, `WITHIN_BUDGET` means every required component is priced
and the complete estimate is at or below budget, and `INCOMPLETE_ESTIMATE`
means affordability cannot be established because costs or currency are
missing or inconsistent. Accommodation, both transport categories, food, and
activities must be priced (or explicitly set to zero) before a plan can be
marked `WITHIN_BUDGET`; other supplied expenses and contingency assumptions
must also be priced. Invalid budgets and negative expenses are rejected.
Different currencies are not added or converted. When known expenses already
exceed the budget despite other unknown costs, the result is still
`OVER_BUDGET`, but the estimated total remains unknown and the overage is
reported as a lower bound. Qualitative Gemini commentary cannot change
calculations/status and is discarded if it contains numeric prices or claims
of affordability. Reduction suggestions do not invent alternative prices;
an optional-activity savings figure is given only when the user supplied
priced activities explicitly marked optional. A `verified` line-item
classification requires a source reference but represents user-supplied
evidence; the agent does not independently verify receipts or sources.

### Phase 6, Step 6: Itinerary Agent

`backend/app/agents/itinerary_agent.py` consumes the shared
`AgentContext.normalized_requirements`, serialized `destination_research`, and
the optional `accommodation_transport_plan` and `budget_breakdown`. It returns
a `TripItinerary` via `AgentResult`. Its Pydantic output types are defined in
`backend/app/schemas/itinerary.py`: `ItineraryActivity`, `DailyItinerary`,
`TripItinerary`, and `ItineraryValidationIssue`.

The agent creates a day for every inclusive trip date, ranks only Geoapify
attractions supplied by destination research, avoids repeat place IDs unless
`constraints.allow_repeat_attractions` is true, and adds clearly marked
arrival/departure buffers, meal breaks, and rest periods. Activity times and
90-minute visit durations are planning placeholders, not provider-verified
opening hours. Attractions retain their provider IDs, verified coordinates,
and matching destination-research source references. When Gemini is configured
it may propose daily themes and attraction ordering by known place IDs only;
unknown IDs, dates outside the trip, duplicate dates, and unsupported fields
are rejected or discarded. Deterministic validation remains authoritative.

Adjacent attraction routing uses the existing OpenRouteService directions tool
and its Redis route cache, only with verified coordinates. The default
`walking` profile can be changed using the supported
`constraints.itinerary_routing_mode` values `driving`, `walking`, or `cycling`.
If routing is unavailable, the relevant travel time remains unknown and the
day is marked as requiring route verification; no zero-duration journey is
assumed. Weather uses only daily forecasts already present in destination
research. On adverse forecast days, provider-listed indoor categories are
prioritized when recognizable; missing or out-of-horizon forecasts remain
unknown and generate warnings.

The budget result must match the request currency and total. An
`OVER_BUDGET` result sets `requires_revision` and prevents the agent from
reporting a viable completed itinerary. An incomplete budget remains
incomplete; the agent never fills unknown activity fees with zero. Daily
spending is shown only when the Budget Agent provides a complete trip total;
it is a labeled equal allocation across trip days, not a day-specific forecast.
Attraction admission prices are not supplied by the current providers and
remain unknown.

Example (fictional, abbreviated):

```json
{
  "destination_name": "Sample City",
  "start_date": "2027-05-01",
  "end_date": "2027-05-01",
  "currency": "INR",
  "budget_status": "WITHIN_BUDGET",
  "estimated_total_cost": "25000.00",
  "days": [
    {
      "date": "2027-05-01",
      "day_number": 1,
      "theme": "History and local discovery",
      "activities": [
        {
          "activity_type": "attraction",
          "title": "History Museum",
          "place_id": "geoapify-place-id",
          "start_time": "09:00:00",
          "end_time": "10:30:00",
          "duration_minutes": 90,
          "coordinates_verified": true,
          "cost_classification": "unknown"
        }
      ],
      "daily_cost": {
        "amount": "25000.00",
        "currency": "INR",
        "classification": "estimated",
        "allocation_basis": "Equal per-day allocation of the complete trip budget estimate"
      }
    }
  ],
  "requires_revision": false
}
```

Schedules are provisional: local hours, actual arrivals/departures, and
transport feasibility should be confirmed before travel. When attraction
research is limited or routes cannot be verified, warnings and validation
issues explain what needs review rather than inventing missing facts.

### Phase 6, Step 7: Optimization & Review Agent

`backend/app/agents/optimization_agent.py` consumes the shared normalized
requirements and the destination research, accommodation/transport plan,
Budget Agent breakdown, and serialized `TripItinerary` from `AgentContext`.
It returns `AgentResult[OptimizationReview]`; the Pydantic contracts
`OptimizationIssue`, `OptimizationSuggestion`, and `OptimizationReview` are
defined in `backend/app/schemas/optimization.py`.

The review reuses the Itinerary Agent's deterministic schedule validator, then
checks request/date agreement, itinerary day coverage, activity time ranges and
overlaps, duplicate attractions, provider-grounded attraction IDs and source
references, weather warnings, route evidence and travel-time feasibility,
supported accessibility/family/activity constraints, and budget consistency.
Money reconciliation uses `Decimal` and compares the budget, required category
subtotals, line items, itinerary total, and daily allocations without currency
conversion. Unknown expenses, mismatched currencies, unresolved required route
feasibility, and unmet supported mandatory constraints block approval.

`approved=true` is returned only when all mandatory deterministic checks pass
and budget status is `WITHIN_BUDGET`. `OVER_BUDGET` and incomplete/unpriced
required costs block approval. `APPROVED` means the supplied evidence and
constraints passed; `REVISION_REQUIRED` means blocking issues were found with
enough data to review; `INSUFFICIENT_DATA` distinguishes missing/invalid
upstream data or unresolved required evidence. Non-blocking weather concerns
and recommendations remain visible as warnings/issues. Gemini may provide a
qualitative summary and general suggestions only. Its structured response is
validated and screened for unsupported factual, price, currency, route, hours,
accessibility, URL, and affordability claims; it cannot alter deterministic
issues, approval, budget status, or savings.

Numerical savings are only retained when they are in the trip currency,
include a pricing basis, and are bounded by explicitly priced optional
activity line items in the Budget Agent result. No alternative hotel, route,
fare, or attraction price is inferred. The review does not execute an
automatic itinerary revision loop. Accessibility approval is conservative:
when a requested accessibility need cannot be evidenced by current upstream
schemas, it remains a blocking issue rather than being assumed satisfied.

Example (fictional, abbreviated):

```json
{
  "approved": true,
  "review_status": "APPROVED",
  "budget_status": "WITHIN_BUDGET",
  "currency": "INR",
  "estimated_savings": null,
  "issues": [],
  "suggestions": [
    {
      "category": "rest",
      "recommendation": "Keep the existing meal and rest breaks between visits.",
      "estimated_savings": null,
      "currency": null,
      "savings_basis": null
    }
  ],
  "warnings": [],
  "source_references": []
}
```

### Phase 7: LangGraph Multi-Agent Orchestration

`backend/app/workflows/travel_graph.py` exposes `async plan_trip(request)`,
which builds and invokes an asynchronous LangGraph `StateGraph`. Workflow
state is typed in `backend/app/workflows/state.py` and carries the original
`TravelRequest`, normalized requirements, each typed agent output, current
stage and status, warnings/errors, source references, revision count, and
timestamped agent execution history. No credentials are stored in the state.

Agents execute in this order:

```text
requirements -> destination_research -> accommodation_transport -> budget
             -> itinerary -> optimization_review
```

Any failed node stops the workflow. Missing or conflicting required request
fields produce `INVALID_REQUEST`; missing minimum research or an incomplete
required cost estimate produces `INSUFFICIENT_DATA`. Only a deterministic
optimization approval with a reconciled `WITHIN_BUDGET` result can produce
`COMPLETED`. An over-budget result or unresolved blocking review produces
`REVISION_REQUIRED`; execution exceptions and timeouts produce `FAILED`.

The review can route to a revision node at most twice by default. Its current
deterministic action is limited to excluding attraction IDs associated with
blocking research-evidence issues; the revised requirements are schema
validated, then the itinerary and review agents run again. Repeated or
unsupported changes terminate with `REVISION_REQUIRED` instead of replaying
identical calls. This revision does not alter prices or budget assumptions.
The graph includes a conditional budget revalidation route for any future
validated revision that changes cost inputs; such a revision must preserve the
original request budget and be supported by validated price data. No
alternative price is created by this workflow.

Agents may be replaced with mocks through `TravelAgentSet`. The default set is
constructed per invocation, and `aclose()` is called on available agent
resources after completion, failure, timeout, or cancellation. The default
workflow timeout is 180 seconds and each agent timeout is 60 seconds; both
timeouts and `max_revisions` are configurable on `plan_trip`. This is a Python
workflow entry point only; it does not add a trip API, database persistence,
checkpointing, or frontend integration.

Example invocation from `backend/`:

```python
import asyncio

from app.schemas.agent_schemas import TravelRequest
from app.workflows.travel_graph import plan_trip

request = TravelRequest(
    origin="Example Origin",
    destination="Example City",
    start_date="2027-06-01",
    end_date="2027-06-02",
    travelers_count=2,
    total_budget="25000.00",
    currency="INR",
)
result = asyncio.run(plan_trip(request))
print(result["execution_status"])
```

`plan_trip` itself is async; production async callers should `await` it rather
than use `asyncio.run`. Run mocked workflow and backend tests from `backend/`:

```powershell
..\.venv\Scripts\python.exe -m pytest tests\test_travel_graph.py -q
..\.venv\Scripts\python.exe -m pytest -q
```

### Phase 8: Trip Planning API and persistence

The API router in `backend/app/api/trips.py` exposes:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/trips/plan` | Validate and queue a new itinerary plan; responds `202 Accepted`. |
| `GET` | `/api/v1/trips/{trip_id}` | Return saved request metadata, budget, itinerary, review, warnings, and errors. |
| `GET` | `/api/v1/trips/{trip_id}/status` | Poll live Redis status with PostgreSQL fallback. |
| `GET` | `/api/v1/trips` | Paginated trip summaries; restricted by default. |
| `DELETE` | `/api/v1/trips/{trip_id}` | Delete trip and related records; restricted by default. |

POST request example:

```json
{
  "origin": "Delhi",
  "destination": "Jaipur",
  "start_date": "2026-11-01",
  "end_date": "2026-11-03",
  "travelers_count": 2,
  "total_budget": "25000.00",
  "currency": "INR",
  "travel_style": "culture",
  "interests": ["history", "food"],
  "constraints": {}
}
```

The response contains `trip_id`, `task_id`, and `status: "queued"`. Poll the
status endpoint, then retrieve the saved result:

```json
{
  "trip_id": "00000000-0000-0000-0000-000000000000",
  "task_id": "00000000-0000-0000-0000-000000000000",
  "status": "queued",
  "message": "Trip request was accepted for asynchronous planning."
}
```

The request is saved as a `travel_preferences` row and a `trips` row before
work is scheduled. The worker then calls the Phase 7 LangGraph workflow.
Itinerary dates and activities are stored in `itinerary_days` and
`itinerary_activities`; per-agent outputs and timestamped execution summaries,
including budget and review results, are stored in `agent_runs`. Existing
unknown costs remain `null` in API responses. Non-null legacy database cost
columns use zero only as a storage sentinel for unknown values; read responses
use saved structured agent output and metadata to restore `null`, not claim a
zero cost. Workflow statuses are saved in PostgreSQL as the durable source of
truth.

Upstash Redis remains best-effort for fast queued/running/completed/failed,
revision-required, and insufficient-data status polling. If Redis is
unconfigured or unavailable, status is returned from PostgreSQL. On terminal
states, the durable database state takes precedence over a stale Redis value.
The status endpoint and health checks do not require Redis.

`POST /trips/plan` now commits a durable job with the complete request in the
existing `agent_runs` table, atomically with the trip. The default
`TRIP_EXECUTION_MODE=database_worker` uses expiring leases and fenced writes.
Each API process runs one worker by default. For a separate worker, set
`TRIP_WORKER_ENABLED=false` on the API and run `python -m app.worker` from
`backend/`. Legacy `TRIP_EXECUTION_MODE=background` remains available but is
not restart recoverable. Deleting a running trip removes its database records and Redis
status; late results check that the trip still exists and cannot recreate it.
Deletion does not interrupt an already-running provider request.

Pagination is bounded with `limit` (1–100) and `offset` query parameters. Trip
history can be enabled independently using `TRIP_HISTORY_API_ENABLED=true`
(enabled in the local `.env.example`). `TRIP_ADMIN_API_ENABLED=true` enables
both listing and deletion for backward compatibility. Both flags default to
false in application settings; do not enable them on a public deployment without an authenticated,
authorized administrative boundary. This project does not yet implement
authentication: trip IDs alone are not authorization to read trip details or
status and are not safe credentials for public production use. Configure
`TRIP_WORKFLOW_TIMEOUT_SECONDS` (default 180),
`TRIP_AGENT_TIMEOUT_SECONDS` (default 60), and `TRIP_MAX_REVISIONS` (default
2) to bound workflow behavior.

Interactive budget revision uses `POST /api/v1/trips/{trip_id}/revise` with
`{"allow_economy_accommodation": false}`. The browser uses the existing status
endpoint until the revised result is saved. Only complete over-budget snapshots
qualify; fixed allowances and unchanged AI unit rates are retained. Economy
accommodation requires explicit consent. The existing `TRIP_MAX_REVISIONS`
setting also bounds interactive attempts per trip; identical no-op requests are
rejected. Results include optional `budget_revision_summary` metadata with
before/after totals, actual reductions, remaining overrun, and explanations.

**Access boundary:** this mutation is local-development-only. It requires
`APP_ENV=development`, a loopback peer and backend hostname, a permitted local
frontend Origin (localhost/127.0.0.1 on 5173/5174), and `X-Trip-Revision: 1`.
Production and remote requests receive 403. Implement authenticated trip
ownership before enabling this endpoint on a public service; do not remove the
boundary or treat CORS/trip IDs as authentication. No database migration is
required; revision snapshots and history use the existing durable job record.

Run the API tests, existing endpoint checks, and full offline backend suite
from `backend/`:

```powershell
..\.venv\Scripts\python.exe -m pytest tests\test_trip_api.py tests\test_health.py -q
..\.venv\Scripts\python.exe -m pytest -q
```

PostgreSQL persistence integration tests require `TEST_DATABASE_URL` pointing
to a dedicated database whose name includes `test`, distinct from
`DATABASE_URL`. Tests create and drop only a temporary schema in that dedicated
test database. Never point it at production Neon.

### External travel information APIs

Phase 5 provides asynchronous HTTPX tools for Geoapify geocoding/nearby places, OpenRouteService driving/walking/cycling directions, Open-Meteo daily forecasts, and Tavily travel searches. Configure `GEOAPIFY_API_KEY`, `OPENROUTESERVICE_API_KEY`, and `TAVILY_API_KEY` in the ignored `backend/.env`; Open-Meteo does not require a key. `EXTERNAL_API_TIMEOUT_SECONDS` controls provider request timeouts (default `10`). Missing credentials, unsupported inputs/dates, provider errors and rate limits raise sanitized typed errors rather than fabricated results.

Normalized provider results are Pydantic schemas with explicit measurement units. ORS receives `[longitude, latitude]` pairs, while its public tool accepts `(latitude, longitude)` tuples. Open-Meteo forecasts are only returned for dates in the current 16-day forecast window; unsupported dates raise `UnsupportedForecastDateError`.

Provider results reuse the Redis cache: geocoding/destinations and places for 24 hours, weather for 30 minutes, routes for 6 hours, and Tavily searches for 1 hour. Cache identifiers include the request parameters and are hashed by the existing cache service. Redis outages are treated as misses/failed cache writes; provider requests still run. Tavily source summaries are marked untrusted; treat them as data, not instructions. No provider content is used to invoke tools or agents in this phase.

### Upstash Redis REST

Configure `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` from the Upstash console in the ignored `backend/.env`. Optional settings are `UPSTASH_REDIS_TIMEOUT_SECONDS` (default `5`) and `REDIS_TASK_STATUS_TTL_SECONDS` (default `86400`). The async REST client is created lazily; missing Redis settings or an unavailable Redis service do not prevent backend startup or `/health` from working. Cache reads degrade to misses and cache writes/invalidation report failure without interrupting callers. Redis operations do not use TCP Redis.

The cache uses deterministic namespaced SHA-256 keys and default TTLs: destinations and places 24 hours, weather 30 minutes, routes 6 hours, and search 1 hour. TTLs can be overridden per operation. Sensitive-field payloads are rejected instead of cached. Cache contents are disposable and are not persistent trip data.

Task status is temporary Redis operational state with `pending`, `running`, `completed`, and `failed` values, and expires after 24 hours by default. Updates use an atomic Redis Lua operation to preserve `created_at` and avoid a read-modify-write race. Concurrent updates are serialized by Redis, but last-writer-wins per field; there is no history, cross-field conflict resolution, or durable recovery. Neon remains the persistent source of truth.

Run offline Redis/backend tests from `backend/` (these tests use in-memory fakes or `httpx.MockTransport`; they never call Upstash):

```powershell
..\.venv\Scripts\python.exe -m pytest -q
```

`CORS_ORIGINS` uses a JSON array, for example `["http://localhost:5173","http://127.0.0.1:5173"]`. The settings object has a method to validate the credentials for an integration when it is enabled in a later phase.

The health check is available at <http://localhost:8001/health>, API status at <http://localhost:8001/api/v1/status>, and interactive docs at <http://localhost:8001/docs>. Run the backend tests from `backend/` using the existing root environment:

```powershell
..\.venv\Scripts\python.exe -m pytest
```

To start Uvicorn from the repository root instead, use the package-qualified import:

```powershell
uvicorn backend.app.main:app --reload --port 8001
```

If running tests from the repository root, change to `backend/` first:

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m pytest
```

### Frontend

In another terminal, from the repository root:

```powershell
cd frontend
npm install
Copy-Item .env.example .env
npm run dev
```

Vite prints the local development URL (normally <http://localhost:5173>).

### Phase 9 frontend

The React app submits the Phase 8 `TripPlanRequest` contract to
`POST /api/v1/trips/plan`, polls `GET /api/v1/trips/{trip_id}/status`, and
loads terminal trip data from `GET /api/v1/trips/{trip_id}`. Set
`VITE_API_BASE_URL` in `frontend/.env` to the backend origin; the committed
example defaults to `http://127.0.0.1:8001`. The frontend displays only
returned itinerary and budget values, retains warnings and review notes, and
does not imply that unknown prices are free.

The planner accepts optional user-provided accommodation, local/intercity
transport, food, and activity cost estimates. These are labeled estimates, not
live quotes; they are used in the selected trip currency without conversion.
Unknown required costs remain unknown and prevent approval. An
`insufficient_data` response still displays available itinerary details,
warnings, and missing-cost guidance. Weather outside Open-Meteo's supported
forecast horizon is marked unavailable and is not fabricated.

The default Gemini text model is `gemini-2.5-flash-lite`. Set `GEMINI_MODEL`
in the local ignored `backend/.env` only if selecting another model supported
by the Google Gen AI API. Provider integrations are covered by mocked tests;
no live provider call is required for local test runs.

Saved-trip listing calls the existing paginated `GET /api/v1/trips` endpoint.
Enable `TRIP_HISTORY_API_ENABLED=true` in the local `backend/.env` to use
the journal without enabling deletion. Restart the backend after changing
environment settings. Disabled history returns a structured 404; unavailable
storage returns 503, and an empty journal returns 200 with an empty `items` list.
Trip IDs are not authentication: do not expose this API publicly without
appropriate access controls. The default workflow uses durable PostgreSQL jobs;
the optional legacy background-task mode is not restart recoverable.

An HTTP 202 planning response means the request was queued, not approved.
Status polling returns HTTP 200 for known trips, including terminal
`failed`, `insufficient_data`, and `revision_required` outcomes. Inspect
`status`, `current_stage`, and `error_message`, then the trip details'
`warnings`, `errors`, and review issues. Unresolved destinations need a
specific city and country. Missing cost preferences leave expenses unknown;
provide planning estimates using the existing cost fields to allow budget
validation. These are user estimates, not live availability or prices.

### Cost preference integration

The optional form fields are serialized as `cost_preferences`, retained by the
Requirements Agent and LangGraph state, used by Accommodation & Transport
and Budget agents, and persisted with the request and agent results. Blank
prices remain unknown; no prices are automatically filled in. Room count
defaults to the existing assumption of at most two travelers per room when
the traveler does not specify it. All calculations use Python `Decimal`.

Trip days include both start and end dates; hotel nights are the date
difference. The room rate is multiplied by rooms and nights, round-trip fare
by travelers, daily local transport by trip days, food by travelers and trip
days, and activities by travelers. An optional contingency percentage is
applied to the supported, priced, same-currency cost base and rounded to two
decimal places using `ROUND_HALF_UP`. When required categories remain unknown,
the reserve is labeled as a partial planning assumption, names the excluded
categories, and does not make the complete trip total available. Unknown
expenses are never treated as zero. If no supported positive base exists,
the percentage reserve remains unknown; an all-zero complete base produces a
zero reserve.

Budget results include `pricing_status` (`COMPLETE_ESTIMATE`,
`LIVE_PRICE_VERIFIED`, or `INCOMPLETE`) and `missing_price_inputs` using the
existing cost field names. Complete user estimates allow affordability review;
they do not verify live prices, availability, routing, or critical itinerary
constraints. Overruns and unresolved critical failures still block approval.

For an illustrative INR estimate for two travelers on October 15–27, 2026:
one room at 5,000/night costs 60,000 for 12 nights; a 30,000 round-trip
allowance per traveler costs 60,000; local transport at 1,000/day costs 13,000;
food at 1,000/traveler/day costs 26,000; activities at 5,000/traveler/trip cost
10,000. The subtotal is 169,000; a 10% reserve is 16,900; the total is
185,900, leaving 14,100 from a 200,000 budget. These numbers illustrate
user-entered allowances, not researched prices. Existing saved requests are
not recalculated or modified automatically.

Run the frontend production build and mocked offline tests in PowerShell:

```powershell
Set-Location frontend
npm install
npm run test
npm run lint
npm run build
```

### Dynamic destination and multi-city planning

Origins, destinations, dates, traveler counts, budget and currency come from each
request; the form does not prefill these values. Named scenario data and prices
in regression tests are isolated fixtures, never application defaults or live quotes.

Destination research retains provider city/region/country metadata. For broad
destinations it discovers up to six cities inside the provider boundary.
Country-level inputs require an explicit city choice: an unranked city/town
shortlist and attraction counts do not establish a suitable national base.
Region-level inputs compare available attractions with interests and duration;
a unique supported match can become a single base. Ties, unavailable boundaries,
missing interest coverage or unresolved important constraints require clarification;
the workflow does not assume a country centroid represents a city or a whole-country tour.
Native location names and accented names are supported by location matching.

Enter an ordered multi-city destination with ` > ` between cities, or supply
`constraints.destination_cities` through the API. Choose a supported routing mode
in cost preferences or `constraints.intercity_routing_mode`. Current multi-city
coverage uses provider driving/walking/cycling routes, at most four city segments,
at least two days per segment, and an eight-hour maximum transfer window.
Each segment uses its own attractions, dates and available weather. The itinerary
reserves provider transfer durations before visits; the reviewer rejects missing
transfers and activities placed in the wrong segment. Returning to an earlier city
preserves the requested order and both transfers.

Flight, train and bus transfer timetables are not integrated. Such multi-city requests,
unavailable routes and transfers outside the planning window request clarification.
Suggested itinerary slots are planning assumptions, not confirmed opening hours or
departure schedules. Provider coverage and forecast date ranges limit live data.
User allowances remain labeled estimates in the selected currency. Automatic
foreign-currency references use a dated, explicitly recorded exchange rate;
failed conversions remain unknown. Multi-city allowances must include
every transport leg; a supplied nightly rate is a uniform allowance across cities.

### Automatic planning cost estimates

Basic trip details can produce a complete planning budget when the reference
registry covers the requested destination, origin route, style and room occupancy.
The Budget Agent uses valid configured Amadeus production flight prices first,
then matching dated records from `backend/data/cost_references.json`, then
user-provided allowances for categories without supported pricing. Room counts,
transport modes and the supplied contingency percentage remain request inputs.
It never asks Gemini to manufacture prices. Missing prices retain null amounts
and category-specific coverage/FX warnings. The registry now includes reviewed
historical spending references for India and Dubai: rooms, food, local transport
and entertainment allowances. These are low-confidence regional/city estimates,
not hotel availability, actual tickets or flight prices. Flight/intercity
references still require exact route coverage or a user allowance.
Gemini explains deterministic category results and tradeoffs; numeric or
unsupported affordability claims in its commentary are discarded.

The shipped dataset is deliberately limited: JNTO Japan double-room/casual-food
references; Tokyo Metro public-transport allowances with a JNTO airport reserve;
Tokyo National Museum admission-based sightseeing allowances; and dated public
JAL/ANA economy fare examples for Hyderabad–Tokyo, Mumbai–Tokyo, Delhi–Tokyo and
Mumbai–Osaka. Fare examples refer to different advertised dates, not the user's
travel dates. Planner multipliers, daily activity counts and reserve percentages
are explicit assumptions, not market statistics or availability claims.
Japan luxury meals, private transfers, premium activities, unsupported routes and
multi-city automatic budgets remain unknown without additional supported data.
Other regions can be supported by adding documented, scoped, dated records;
the application contains no destination-specific estimation branch or sample
trip defaults. Configurable data does not imply worldwide pricing coverage.

Reference FX first uses Frankfurter's public central-bank rate API, then the
ECB official daily XML feed if necessary. Currency pairs, positive finite rates
and dates are validated; observations older than seven days are rejected.
ECB cross rates divide two rates from the same published date using Decimal.
No guessed FX rate is substituted. Retail conversion fees remain unverified.

New optional settings (no credentials have been added or changed):

- `AUTOMATIC_COST_ESTIMATION_ENABLED` (default `true`).
- `COST_REFERENCE_FILE` (default the bundled JSON registry).
- `DEFAULT_ESTIMATE_CONTINGENCY_PERCENT` (default `10`, used when automatic
  estimates are added or essential base costs remain unknown and no reserve was
  supplied). Percentage reserves remain unknown until the base is complete.
- `REFERENCE_FX_ENABLED` (default `true`) and `REFERENCE_FX_MAX_AGE_DAYS` (`7`).
- `AMADEUS_API_KEY`, `AMADEUS_API_SECRET`, `AMADEUS_ENVIRONMENT` (`test` by default).
  Production requires authorized production credentials. Sandbox offers are
  discarded, not promoted to prices. Only origin-to-base round-trip economy
  flight search is implemented; there is no live hotel-pricing adapter.

All totals use Decimal with half-up rounding to two decimal places. Days are
inclusive; hotel nights are end date minus start date. Room count is supplied
or derived from the existing occupancy rule. Ranges are scenario allowances,
not guaranteed market minima/maxima. Supplied fixed allowances remain fixed
within a mixed scenario range. Unknown costs are null and block a complete
total; `known_cost_subtotal` and `missing_price_actions` provide a useful partial
budget. Reference snapshots expire and require human review/refresh; they are
not scraped or repriced automatically.

Additive result fields include line-item `evidence_kind` (`planning_estimate`,
`verified_price`, `unknown`), bounds, confidence, source observation dates,
assumptions and FX provenance. Budget results include scenario totals and
`booking_readiness: unverified`. A complete estimate can pass planning review,
but budget overruns, missing essential costs and critical itinerary failures
still prevent approval. Existing persistence saves this metadata without a
database migration. Existing stored trips are not recalculated automatically.

The responsive budget card shows totals, range, remaining budget/overrun,
confidence, sources, assumptions, missing-price actions and booking warnings.
Trip details also include optional `accommodation_transport` output from the saved
agent execution, so stay categories, areas, rooms/nights, transport recommendations
and provider route facts can be shown without implying hotel availability or
flight/rail/bus schedule verification. Broad country/region origins need a departure
city for route-specific pricing; their geographic centroids are not departure points.
Provider country codes and long country-name initialisms are handled consistently
by geocoding and agent validation, while wrong-country qualifications remain rejected.
The frontend retains unknown totals and detects inconsistent saved subtotals rather
than hiding valid category prices. See [the repair report](MISSING_COST_FIX_REPORT.md)
for actual read-only API findings, regression scenarios and local verification.
For the live/offline checks and limitations, see
[the automatic estimation report](AUTOMATIC_COST_ESTIMATION_REPORT.md).

### Database schema and migrations

Phase 10 Stage C adds provider retries, safe structured diagnostics, Gemini JSON
Schema compatibility, request-sized itinerary schemas, and stale-job recovery.
See [the reliability audit report](PHASE10_STAGE_C_REPORT.md) for confirmed root
causes, exact changed files, live-check results, and remaining limitations.
`EXTERNAL_API_MAX_RETRIES` defaults to two (three total attempts). Retries apply
to timeouts, network failures, rate limiting and server errors; authentication,
bad requests and response-validation errors are not retried. Retry-After delays
outside the bounded five-second retry window return the error immediately.
Gemini SDK retries are disabled; the existing application retry limit remains
authoritative. Provider results and deterministic planning survive optional AI,
search and weather failures. Incomplete prices and critical route failures still
block reviewer approval.

Legacy background-task status checks detect expiry and report a terminal failure.
Durable jobs instead recover after an expired lease; status reads do not destroy
recoverable work. PostgreSQL row locks and tokens fence stale workers out of
result/failure writes. Stage snapshots preserve earlier valid outputs on failure.
The current verification report distinguishes the tested local SQL restart path
from PostgreSQL concurrency tests that require a dedicated test database:
[permanent repair evidence](PERMANENT_FIX_REPORT.md).

Live checks are opt-in and disable PostgreSQL/Redis access and retries:

```powershell
Set-Location backend
..\.venv\Scripts\python.exe scripts/provider_smoke.py --destination "your city, country"
```

Optionally add `--request-json PATH` to exercise the six real agents with a normal
trip request payload. The fixture in `backend/tests/fixtures/` is a test scenario,
has no prices, and is never an application default. Live calls consume provider
quota; do not run the smoke script as part of the offline test suite.

The SQLAlchemy engine and session factory are created lazily when the database dependency or repository is used. Health and status endpoints do not require database availability. Application startup does not create tables, and Alembic migrations are never run automatically.

Review the migration from `backend/` without applying it:

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m alembic history
..\.venv\Scripts\python.exe -m alembic upgrade head --sql
```

Only after reviewing the generated SQL and confirming `DATABASE_URL` targets the intended database, apply migrations manually:

```powershell
..\.venv\Scripts\python.exe -m alembic upgrade head
```

Do not use the production `DATABASE_URL` for integration tests. To opt in, set `TEST_DATABASE_URL` to a separate PostgreSQL database whose name contains `test`. Integration tests use a uniquely named temporary schema and remove only that schema. With no test database configured, those tests are skipped.

The `.env.example` files contain placeholders only. Replace them with your own values; never commit a real `.env` file. Phase 6 Step 1 does not implement LangGraph workflows, agent orchestration, or itinerary generation.
