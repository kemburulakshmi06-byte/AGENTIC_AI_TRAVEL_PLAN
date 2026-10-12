> Historical report from the earlier repair pass. The current cost coverage, durable worker changes and verification results are documented in [PERMANENT_FIX_REPORT.md](PERMANENT_FIX_REPORT.md). Earlier limitations below describe the state at that time.

# Missing trip costs and itinerary details — repair report

## Confirmed causes and limits

The automatic estimator was already invoked by the Budget Agent. Local settings
enable automatic estimation and reference FX, and the configured reference file
exists. The bundled registry contains ten Japan records only. There are no Dubai
records in this checkout. Amadeus credentials are not configured locally. Gemini,
Geoapify, routing and web research integrations do not provide a general verified
hotel/flight/food pricing feed. They must not be used to manufacture quotations.

Consequently, an unsupported destination can correctly have five unknown expense
categories, a null total and insufficient_data status. Unsupported routes, travel
styles, expired records and unavailable currency conversion can also leave costs
unknown. A single missing essential category prevents a complete total. No new
market prices or exchange rates were added.

The follow-up audit on October 12, 2026 identified the actual reported trip through
read-only history/detail requests: `57d5f15c-fb08-4c2c-b527-3c763a8b3a7b`, Hyderabad
to Bengaluru, October 13–17, two travelers, INR 20,000, balanced style, no cost
preferences. The status and detail APIs return 200, insufficient_data, a null total
and five itinerary days. This is a pricing-coverage failure, not a missing route,
queued job, failed Gemini response or missing itinerary record.

Read-only inspection of its AgentRun records confirms all six agents executed:
requirements completed; destination research completed with country `in` and 20
verified attractions; accommodation/transport completed; budget needs_input;
itinerary needs_input; optimization completed with an insufficient-data review.
The saved accommodation/transport output includes one room, four nights, walking,
local-transit and driving recommendations, with one provider-routed option. The
workflow ran for approximately 78.79 seconds. Raw provider HTTP logs were not
available, so no claim is made about an unseen provider response body.

The API and database inspections did not modify production records. The updated
API readback used a PostgreSQL `SET TRANSACTION READ ONLY` transaction and rolled
it back. No live pricing provider was called and no new production trip was
submitted. New POST/background/poll/detail checks use an isolated test app and
captured SQLAlchemy objects, with explicitly fictional provider fixtures.

## Execution trace

1. `frontend/src/components/PlannerForm.tsx` constructs the dynamic request and
   optional cost preferences. `frontend/src/api.ts` submits `/api/v1/trips/plan`.
2. `backend/app/api/trips.py` validates `TripPlanRequest`, saves the request using
   `TripPlanningService.create_trip`, schedules `_run_and_close` through FastAPI
   BackgroundTasks, and returns queued/202. This is acceptance, not completion.
3. `backend/app/workflows/travel_graph.py` passes normalized requirements,
   destination research, accommodation/transport outputs, budget and itinerary
   through the existing six agents. Destination clarification can legitimately
   stop execution before pricing. Budget needs_input does not discard itinerary
   generation; the reviewer prevents incomplete budgets from being approved.
4. `backend/app/agents/budget_agent.py` invokes `CostEstimationService` and combines
   priced categories with retained unknown categories using Decimal calculations.
   FX failure never becomes a guessed conversion or free expense.
5. `TripPlanningService._persist_result` saves the final budget and itinerary.
   Agent results are JSON in AgentRun; available days/activities remain persisted
   for insufficient_data results. API retrieval restores unknown activity/day
   costs from the existing metadata markers rather than exposing legacy storage
   zeros as actual prices. Status polling consults persisted state.
6. `TripWorkspace.tsx` fetches status then details and displays totals, sources,
   assumptions, warnings and available persisted itinerary days.

## Repairs

| File | Repair |
| --- | --- |
| backend/app/services/cost_estimation_service.py | Validate geographic research separately from reference data, so a broken registry cannot erase the resolved city/country used by pricing. Validate provider quote category, currency and verified evidence. Identify disabled estimation, invalid registry, missing country, unsupported country/city/route/style/mode/occupancy, expired/ambiguous references and FX failures using safe structured diagnostic events. Reject reference accommodation with insufficient room capacity. |
| backend/app/agents/budget_agent.py | Apply provider/reference/allowance priority; preserve original request fields and use allowances when supported pricing is unavailable. Remove missing-price actions for categories successfully resolved by allowances. Retain unknown percentage contingency when base costs are missing. Handle zero hotel nights without requiring an accommodation quote. Report medium reference confidence correctly and log incomplete budget outcomes. |
| backend/app/workflows/travel_graph.py | Accept harmless repeated whitespace in locations without accepting changes to actual origin/destination/date/count/budget/currency. Snapshot each successful agent execution's own output. |
| backend/app/workflows/state.py | Add internal optional execution output snapshots without a database migration or public API change. |
| backend/app/services/trip_planning_service.py | Persist each execution snapshot instead of copying final revision output into every prior AgentRun. Retain compatibility with older execution records. |
| frontend/src/components/TripWorkspace.tsx | Retry transient terminal-detail fetch failures, treat persisted planned status as terminal, retrieve available details for failed trips, avoid a ready message before details load, and label excluded categories as not included instead of displaying a zero expense. |
| frontend/src/components/PlannerForm.tsx | Explain automatic pricing attempts and fallback allowances accurately, including source priority. |
| backend/tests/test_missing_cost_regressions.py | New isolated regression tests for the defects and diagnostic rules. |
| backend/tests/test_automatic_cost_estimation.py | Update priority/reserve expectations and verify complete and partial ORM persistence/retrieval, including itinerary days and unknown daily costs. |
| frontend/src/components/TripWorkspace.test.tsx | Test transient retrieval recovery, legacy planned status, unknown contingency, failed-trip details and retained itinerary display. |
| README.md | Document implemented source priority, contingency behavior and actual coverage. |
| MISSING_COST_FIX_REPORT.md | This report. |

Additional changes from the complete follow-up request:

| File/function and location | Repair |
| --- | --- |
| backend/app/services/trip_planning_service.py, `_get_trip`, line 485 | Return persisted accommodation_transport agent output, and expose original cost_preferences from the existing constraints storage. No record rewrite or recalculation. |
| backend/app/schemas/trip_api.py, `TripDetailsResponse`, line 114 | Add optional accommodation_transport detail output; existing response fields remain. |
| backend/app/schemas/accommodation_transport.py, `AccommodationTransportPlan` | Add origin resolution metadata to prevent a known country/region origin from becoming an arbitrary departure point. |
| backend/app/agents/accommodation_transport_agent.py, `_resolve_coordinates`, `_location_matches` | Identify broad origins, avoid routing from their centroids, and require the complete origin qualification to match rather than accepting any shared token. |
| backend/app/tools/geoapify.py, `_destination_match_score`, line 345 | Recognize provider country codes and long country-name initialisms, including UAE derived from United Arab Emirates. Do not derive city aliases or accept a different two-letter country's code. |
| backend/app/agents/destination_research_agent.py, `_geocode_matches`, line 672 | Use the same qualified geographic matcher as provider geocoding. This fixes the reproduced Dubai,UAE rejection while retaining wrong-country rejection. |
| backend/app/services/cost_estimation_service.py, `estimate_missing`, line 145 | Skip live flight searches for provider-confirmed broad origins and request a departure city or whole-trip allowance. |
| frontend/src/types.ts, line 103 | Accept the additive optional accommodation_transport detail field. |
| frontend/src/components/TripWorkspace.tsx, `TripResult`, line 219; `StayAndTransport`, line 479 | Render stored stay/transport recommendations and provider route facts, preserve unverified availability/schedules, avoid saying no priced expenses when priced items exist, flag inconsistent subtotals, and prefer explicit budget totals—including null—over stale trip totals. Incomplete data cannot display a complete total/range/remaining-budget claim. |
| backend/tests/test_full_trip_request_flow.py | Test the reported INR 20,000 request and scenarios A–J through actual FastAPI routes, background scheduling, planning service, six-agent graph and ORM serialization, with isolated test providers/storage. Also test worker restart expiry and geographic qualification. |
| backend/tests/test_automatic_cost_estimation.py | Assert complete/partial readback includes the saved accommodation/transport result. |
| frontend/src/components/TripWorkspace.test.tsx | Add stay/transport, priced-item/zero-summary inconsistency and stale incomplete-total regressions; correct old fixtures that encoded misleading summaries. |

CORS, history routes, credentials, provider keys and production records were not
changed. No commit, push, deployment or migration was performed. Inputs remain
dynamic; sample destinations appear only in tests and existing scoped data.

## Calculation policy

Days are inclusive: `(end_date - start_date).days + 1`; hotel nights are
`(end_date - start_date).days`. Accommodation uses supported room occupancy and
rooms × nights; supplied round-trip fare uses travelers; local daily allowance
uses days; food uses travelers × days; activities allowance uses travelers.
Final amounts use Decimal and half-up rounding to two decimals.

Percentage contingency requires a complete base subtotal. If essential costs
are missing, it is null, with an explanation; it is not silently zero. A fixed
explicit contingency can remain priced independently. When automatic estimates
are added, or essential base costs remain unknown, the configured default reserve
is represented unless the user supplied a reserve. A fully supplied manual budget
without a reserve retains its existing calculation behavior.

An isolated allowance-fallback regression uses two travelers, five inclusive days,
four nights, one room, nightly rate 100.01, local daily allowance 20.01, per-person
round-trip fare 300.01, daily food 30.01 and activities 40.01. The subtotal is
1480.23, the 10% reserve is 148.02 and total is 1628.25. These are synthetic test
inputs, not travel prices. Reference/FX tests also use explicitly test-only provider
fixtures; no fixture is used in production.

## Final verification

- Backend: `python -B -m pytest -q -p no:cacheprovider` — **436 passed, 2 skipped**.
- Frontend: `npm test -- --reporter=dot` — **42 passed**, four files.
- Frontend: `npm run build` — TypeScript and Vite production build **passed**.
- Frontend: `npm run lint` — **passed**.

Database integration tests were skipped because a dedicated TEST_DATABASE_URL was
not supplied. Provider/network behavior is tested with isolated fixtures and
mock HTTP transports; these tests do not prove current booking availability or
production database write behavior. The separate read-only audit confirmed existing
database records and API readback. Initial regression runs identified expectations
changed by the requested source priority and contingency policy, plus an excluded
category rendering defect; these were corrected before the final passing runs.
The sandbox initially blocked Vite's Windows dependency resolver with EPERM;
the authorized checks succeeded outside that restriction. One lint invocation
used the repository root; it was corrected to the frontend directory.
Follow-up test development caught pytest's reserved request parameter name and a
root-directory import invocation; both were corrected. The scenario matrix exposed
the real UAE qualification defect, which was fixed in shared geographic validation.
Frontend failures exposed inconsistent old fixtures and the need to suppress stale
complete totals/ranges; the final suites have no failed tests.

## Actual API findings and before/after readback

| Read-only API case | HTTP/status | Costs and itinerary |
| --- | --- | --- |
| Actual reported Hyderabad → Bengaluru, INR 20,000 | Detail/status both 200; insufficient_data, optimization_review stage | Five days saved; all five base costs null; known subtotal 0.00; confidence unassessed; no allowances and no India references. |
| Actual saved country-origin India → Tokyo | Detail/status both 200; insufficient_data | Five days; known subtotal INR 96,431.11, from four stored reference categories; intercity fare and contingency unknown. Country origin lacks a departure city/route reference. |
| Actual saved country-origin India → Dubai, AED | Detail/status both 200; insufficient_data | Five days; all base prices unknown. No UAE references; FX was not the initial blocker because there was no source amount to convert. |
| Actual saved Hyderabad → Tokyo with user allowances | Detail/status both 200; completed | Three days; INR 98,670.00 total, labeled user assumptions. This is not evidence of automatic live pricing. |

The last saved trip's user-allowance breakdown is accommodation INR 8,000, local
transport 4,500, intercity transport 60,000, food 7,200, activities 10,000 and
contingency 8,970. These are existing user estimates, not independently verified
prices or newly inserted records.

For the reported Bengaluru record, the original running API omitted the
accommodation_transport field. A fresh instance of the updated FastAPI application,
reading that exact production record under a read-only PostgreSQL transaction,
returned 200 with that field populated: one room, four nights, three transport
options and one routed option. The five itinerary days and incomplete/null budget
remain unchanged. Restart the running backend to load the additive API change;
this check did not restart the user's server or rewrite its old budgets.

New pricing and contingency behavior was checked through isolated POST/202 →
background work → status/200 → details/200 requests. It was not inserted into the
production database. This distinction is essential: an old saved budget is not a
newly recomputed result.

## Requested scenario coverage

The following use the real API/service/graph and isolated ORM/provider fixtures.
They are not live destination research or production database write tests.

| Scenario | Result checked |
| --- | --- |
| Reported Hyderabad → Bengaluru, two travelers, five days, INR 20,000, no allowances | insufficient_data, five days retained, all base costs and percentage contingency unknown; specific country-coverage warnings. |
| A: Hyderabad, India → Tokyo, Japan | Complete reference estimate but INR 20,000 budget overrun; revision_required, never approved. |
| B: Hyderabad, India → Dubai, UAE | Geographic qualification succeeds; missing UAE references remain insufficient_data, with itinerary retained. |
| C: India → Tokyo | Partial reference budget; broad origin requires a departure city/airport or transport allowance. |
| D: India → Dubai, AED | Broad origin and missing UAE pricing remain actionable unknowns. |
| E: Domestic Hyderabad → Bengaluru with explicit allowances | completed planning estimate; domestic support here uses allowances, not a nonexistent India rate table. |
| F: Manually supplied Dubai allowances | completed planning estimate with explicit user sources and unverified booking readiness. |
| G: No available pricing sources | Null complete total, unknown reserve, specific missing-price warnings. |
| H: External flight-pricing timeout | Supported reference fallback retains source labels; private provider body suppressed. |
| I: Worker exception or fresh service after lost queued work | failed status via polling; no fake plan/price; timeout recovery does not resume lost work. |
| J: Very low Tokyo budget | Overrun remains revision_required and unapproved. |

For E/F, the synthetic user inputs yield accommodation INR 8,000 (2,000 × one room
× four nights), local transport 1,000 (200 × five days), intercity 2,000 (1,000 ×
two travelers), food 3,000 (300 × two × five), activities 1,000 (500 × two), reserve
1,500 (10% of 15,000), total 16,500 and remaining budget 3,500. These are test
allowances, not market prices.

For A/H/J, the test-only JPY/INR conversion of 0.60 yields accommodation 48,000,
local transport 15,180, intercity 257,064, food 19,560, activities 12,000 and
reserve 35,180.40, totaling 386,984.40. The modeled range is 297,655.60–572,589.60.
The exchange rate is explicitly fictional for deterministic tests; do not use this
example as an actual travel quote. Existing sourced reference records, dates,
room/night/day multipliers and price evidence remain preserved.

## Implemented data sources and remaining limitations

- Existing dated JNTO/Japan hotel and food guides, Tokyo Metro allowances,
  Tokyo museum admission-based activity allowances and scoped JAL/ANA fare
  snapshots are planning references, not actual-date quotes.
- Optional authorized Amadeus production round-trip flight search is implemented;
  sandbox prices remain unusable as real prices. No live hotel-price adapter exists.
- Frankfurter central-bank reference FX with ECB daily-feed fallback is implemented.
  Missing or stale pairs, including AED when absent from a feed, stay unknown.
- User allowances supply otherwise unsupported categories with explicit sources.
- No Dubai reference prices were invented or added. Unsupported routes/regions,
  luxury/premium costs and multi-city budgets need suitable data or allowances.
- Booking readiness remains unverified. Overruns and critical itinerary failures
  cannot be approved merely because estimates are complete.
- FastAPI BackgroundTasks remain in-process and are not durable across abrupt
  server termination. A durable worker remains a separate deployment requirement.
- Existing saved trips are not recalculated. Refresh also still loses the current
  trip selection held in application memory; it does not recalculate a saved trip.

## Manual verification

1. Restart the backend using the existing development command, then refresh Vite.
2. Open `http://127.0.0.1:8001/docs`. Submit a **new**, city-specific trip through
   POST `/api/v1/trips/plan`; preserve the returned trip_id. Use dates, travel
   style, room occupancy and a route actually covered by the reference registry
   to check automatic estimates. Reference snapshots must still be current.
3. Poll GET `/api/v1/trips/{trip_id}/status` until terminal. A complete affordable
   budget alone does not guarantee completed status; verified research/routing
   and other reviewer constraints must also pass.
4. Retrieve GET `/api/v1/trips/{trip_id}`. Check budget.line_items, pricing_status,
   source dates, currency_conversion, estimated_total, days, itinerary,
   optimization_review and warnings/errors. Compare this same trip ID in the UI.
5. Test an unsupported destination without allowances: expect unknown essential
   categories, null complete total, unknown percentage contingency and specific
   warnings. Available verified itinerary days should still display when generated.
6. Supply explicit allowances for that unsupported trip and submit a new request:
   check Decimal totals and labeled sources. Never expect missing provider data
   or a genuine budget overrun to be changed into an approved plan.

No manual submission was made against the production-backed local API during this
repair because doing so would create/change production database records. New
scenario submissions were run only in the isolated regression app.

To load changes locally, stop the existing backend in its terminal with Ctrl+C,
then from the project root run:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8001 --reload
```

Keep the existing Vite terminal running for source reload, or restart from frontend:

```powershell
npm run dev -- --host 127.0.0.1 --port 5175
```

Read the existing trip through GET first to verify saved stay/transport details
without changing data. Run regression POST scenarios against isolated test storage.
Fresh manual Swagger submissions create records in whichever database is configured;
use dedicated test storage for regression submissions. No credential/configuration
change or server restart was performed automatically by this repair.
