# Master end-to-end repair report — 12 October 2026

## Execution map

The implemented request path is:

1. `frontend/src/components/PlannerForm.tsx::handleSubmit` validates and builds the dynamic request.
2. `frontend/src/api.ts::planTrip` posts it to `POST /api/v1/trips/plan`.
3. `backend/app/schemas/trip_api.py::TripPlanRequest` validates the required dates, travelers, budget and currency while preserving optional cost preferences.
4. `backend/app/api/trips.py::create_trip_plan` calls `TripPlanningService.create_trip`.
5. `backend/app/services/trip_planning_service.py::_create_trip` commits `TravelPreference`, `Trip`, and the complete `planning_job` payload in one transaction.
6. `backend/app/main.py::create_app` starts the managed worker, or a separate process runs `backend/app/worker.py::run_worker`.
7. `backend/app/services/trip_job_service.py::TripJobService.claim` claims one queued/expired job with a row lock, lease, attempt limit and ownership token.
8. `backend/app/workflows/travel_graph.py::plan_trip` streams state from the graph created by `build_travel_graph` and checkpoints completed stages.
9. The graph runs Requirements → Destination Research → Accommodation & Transport → Budget → Itinerary → Optimization/Reviewer. Its `revision` node is deterministic workflow control, not a seventh agent.
10. `TripPlanningService._persist_result` fences the write by lease ownership and saves agent output, budget, itinerary rows, workflow diagnostics and terminal status to PostgreSQL.
11. `GET /api/v1/trips/{trip_id}/status` and `GET /api/v1/trips/{trip_id}` read the durable database result. Redis accelerates status but is not authoritative.
12. `frontend/src/components/TripWorkspace.tsx` polls by the returned trip ID, rejects mismatched responses and renders complete or partial saved results.

## A. Root causes

1. **The estimator ran, but its former source registry could not price the reproduced trip.** `backend/app/agents/budget_agent.py::BudgetAgent.run` called `backend/app/services/cost_estimation_service.py::CostEstimationService.estimate_missing`. The registry had Japan coverage only, no user allowances were present, Amadeus was unconfigured, and no live hotel adapter exists. Every Bengaluru category therefore remained unknown. This was a source-coverage failure, not an agent-invocation failure.

2. **Missing Amadeus configuration disabled useful independent estimates in the old behavior.** The estimator now treats flight pricing independently. It can retain source-backed accommodation, food, local transport and activity estimates while leaving the unsupported intercity fare null with a specific action. It never changes an unknown fare to zero.

3. **Planning work depended on process-local `BackgroundTasks`.** A restart could strand a running request. New requests now store a durable `planning_job` in the existing `agent_runs` table and use leased, token-fenced claims with bounded retries. No production schema or record was changed.

4. **A graph exception could discard already completed stages.** The workflow previously obtained state only after `ainvoke` returned. It now consumes `astream` snapshots. Checkpoint writes preserve valid earlier outputs and itinerary rows when a later stage or retry fails.

5. **Frontend selection and polling were not sufficiently correlated.** A refresh lost the selected trip, an older request could update the new view, and a temporary polling error could hide persisted content. The selected validated ID is now kept in session storage, history rows can be opened, responses must match the active ID, and transient requests retry without clearing a saved plan.

6. **Incomplete costs could appear as zero.** Persisted activity/day numeric columns retain schema-compatible zero values, but explicit unknown metadata and the raw itinerary restore API nulls. The UI now prints “Not available” or “No priced amount” for unknown line items and contingency. Known-cost overruns remain visible without constructing a false complete total.

7. **Saved detail responses omitted useful agent output.** `TripDetailsResponse` now includes additive `normalized_requirements` and `destination_research` fields along with the existing accommodation/transport, budget, itinerary and review data.

8. **A generic environment variable could prevent application startup.** The host currently injects `DEBUG=release`. Pydantic tried to parse that as the former boolean `DEBUG` setting, so test collection and FastAPI import failed. The application setting is now `APP_DEBUG`; unrelated machine-level `DEBUG` values are ignored. A regression test covers this exact collision.

Structured diagnostics in `backend/app/core/diagnostics.py` correlate the trip/stage and report output presence, counts and safe error categories. They omit request prose, connection strings, provider bodies and credentials.

## B. Agent verification

There are exactly six agents. The live isolated replay persisted these results:

| Agent | Source | Persisted status | Output persisted |
|---|---|---:|---:|
| Requirements | `backend/app/agents/requirements_agent.py::RequirementsAgent` | completed | Yes |
| Destination Research | `backend/app/agents/destination_research_agent.py::DestinationResearchAgent` | completed | Yes |
| Accommodation & Transport | `backend/app/agents/accommodation_transport_agent.py::AccommodationTransportAgent` | completed | Yes |
| Budget | `backend/app/agents/budget_agent.py::BudgetAgent` | completed, valid partial budget | Yes |
| Itinerary | `backend/app/agents/itinerary_agent.py::ItineraryAgent` | needs_input because a required fare was unknown | Yes, five days |
| Optimization/Reviewer | `backend/app/agents/optimization_agent.py::OptimizationAgent` | needs_input | Yes, approval blocked |

The revision node can route back to Budget or Itinerary and is bounded by `TRIP_MAX_REVISIONS`. It does not call an independent agent or create a seventh persisted agent identity.

## C. API and infrastructure verification

| Integration | Classification | Evidence and limit |
|---|---|---|
| Gemini Developer API | WORKING | Configured; real replay completed structured generation. Gemini prose is not accepted as price evidence. |
| Geoapify | WORKING | Configured; real geocoding and places calls completed. Ambiguous/broad resolution remains explicit. |
| OpenRouteService | WORKING | Configured; real walking-route call completed. Unsupported routes remain unverified. |
| Open-Meteo | WORKING | Real forecast call completed where requested dates were in provider coverage. Out-of-range dates are labeled unsupported. |
| Tavily | WORKING | Configured; real research call and source extraction completed. |
| Amadeus pricing | CONFIGURATION MISSING | API key and secret are absent. No live flight quote is claimed. Sandbox/test offers are not treated as booking evidence. |
| Frankfurter / ECB reference FX | NOT TESTED live in the replay | The replay used same-currency INR references. Validation, stale-rate, fallback and failure behavior have deterministic tests; no live FX success is claimed here. |
| Cost reference registry | WORKING | 34 validated dated rows: Japan 10, India 12 and UAE/Dubai 12. They are non-live planning references with scope and expiry checks. |
| Neon PostgreSQL | WORKING for read connectivity | Read-only `SELECT 1` passed on 12 October 2026. Existing trip detail/status reads also returned HTTP 200. No production write or migration was performed. |
| Upstash Redis | WORKING | Read-only `PING` returned `PONG` on 12 October 2026. PostgreSQL remains the durable authority. |

Configured-state checks disclosed only `configured`/`missing`; no values were printed. The production `.env` files, keys and credentials were not edited.

## D. Pricing verification

Pricing precedence is validated as live provider quote → matching dated scoped reference → user allowance → an explicitly permitted planning assumption. Every monetary item stores evidence kind, source, source date, assumptions, confidence and currency. Final arithmetic uses `Decimal`.

The real replay for Hyderabad → Bengaluru, two travelers, five inclusive days and four nights produced:

| Category | Amount | Classification/source |
|---|---:|---|
| Accommodation | INR 11,568.00 | Low-confidence historical India planning reference; one room × four nights |
| Local transport | INR 5,430.00 | Historical planning reference; two travelers × five days |
| Intercity transport | Unknown | No Amadeus credentials or matching dated route reference |
| Food | INR 14,340.00 | Historical planning reference; two travelers × five days |
| Activities | INR 8,720.00 | Historical planning allowance with stated purchase assumption |
| Contingency | Unknown | Calculated only when all required base categories are known |
| Known subtotal | INR 40,058.00 | Sum of known categories |
| Complete total / remaining | null / null | Correctly incomplete |
| Known overrun against INR 20,000 | INR 20,058.00 | Unknown expenses may increase it |

The automated complete Tokyo reference scenario uses a deliberately deterministic test FX fixture and is not live evidence. It verifies accommodation INR 48,000.00, intercity INR 257,064.00, local INR 15,180.00, food INR 19,560.00, activities INR 12,000.00, contingency INR 35,180.40 and total INR 386,984.40. The manual-allowance six-agent scenario verifies the user formulas across 13 days/12 nights and either blocks an INR 207,900.00 overrun or approves the INR 185,900.00 affordable variant. Those amounts are fixtures/user assumptions, not market quotes.

## E. Before and after

Before the repair, saved trip `57d5f15c-fb08-4c2c-b527-3c763a8b3a7b` returned status `insufficient_data`, total `null`, known subtotal `0.00`, all five base categories unknown, unassessed confidence and no honest contingency line. Five itinerary days and all six agent runs were nevertheless present.

After the repair, a new real-provider isolated replay returned four labeled estimates, known subtotal INR 40,058.00, low confidence, explicit missing intercity fare and contingency, known overrun INR 20,058.00, five persisted days and a reviewer-blocked `insufficient_data` status. This is useful partial output without fake success. The original production record was not rewritten.

## F. Files modified

Core repair files:

- `.gitignore`: protects isolated test/live-check artifacts.
- `backend/app/core/config.py`, `backend/.env.example`: durable-worker, estimation, FX, optional pricing settings and collision-safe `APP_DEBUG`.
- `backend/app/core/diagnostics.py`: safe structured state diagnostics.
- `backend/app/main.py`, `backend/app/worker.py`: managed durable worker lifecycle.
- `backend/app/api/trips.py`: durable scheduling while preserving optional legacy background mode.
- `backend/app/services/trip_job_service.py`: atomic claim, lease, recovery and ownership fencing.
- `backend/app/services/trip_planning_service.py`: atomic trip/job creation, checkpoints, persistence and expanded retrieval.
- `backend/app/workflows/travel_graph.py`: streamed state and checkpoint callback.
- `backend/app/services/cost_estimation_service.py`, `backend/data/cost_references.json`: independent, sourced estimation and expanded reviewed coverage.
- `backend/app/schemas/trip_api.py`: additive persisted agent-output fields.
- `backend/scripts/verify_trip_pipeline.py`: opt-in isolated real-provider verification harness.
- `frontend/src/App.tsx`, `frontend/src/api.ts`, `frontend/src/types.ts`: persisted selection and correlated API data.
- `frontend/src/components/TripWorkspace.tsx`: partial-cost, source, warning, overrun and itinerary rendering.

Regression and documentation files:

- `backend/tests/test_durable_trip_jobs.py`, `test_database_integration.py`, `test_full_trip_request_flow.py`, `test_automatic_cost_estimation.py`, `test_trip_api.py`, `test_health.py`.
- `frontend/src/App.test.tsx`, `frontend/src/components/TripWorkspace.test.tsx`, and the captured non-secret response fixture.
- `README.md`, `MISSING_COST_FIX_REPORT.md`, `PERMANENT_FIX_REPORT.md`, and this report.

## G. Test results

Final checks on 12 October 2026:

- Backend: **453 passed, 0 failed, 4 skipped** in 13.97 seconds.
- Frontend: **48 passed, 0 failed** across four files.
- Frontend lint: passed (`oxlint`).
- TypeScript and Vite production build: passed; 28 modules transformed.
- Alembic offline upgrade SQL: generated in the earlier verification; no database was contacted or migrated.

The four skipped tests require a separate `TEST_DATABASE_URL`: two existing PostgreSQL persistence tests and two PostgreSQL concurrent-claim/expired-lease fencing tests. The suite refuses to use the production database for them.

An initial backend invocation exposed and led to the `DEBUG=release` fix. Later sandboxed attempts were denied access to Windows temporary directories; the same unchanged suites passed when executed with the required OS permissions. Initial Vite/Vitest attempts similarly hit sandbox `spawn EPERM`; their unrestricted reruns passed. These were execution-environment failures, not concealed test assertions.

Regression coverage includes all 20 requested classes: Tokyo, Dubai, Goa, broad India origins, complete references, manual allowances, missing Amadeus, timeouts, missing FX, overrun, one missing category, Gemini failure, agent failure, database failure, worker interruption/recovery, polling recovery, sequential trips, concurrent trips and retrieval after restart.

## H. Real execution evidence

- Real-provider replay ID: `ada9d07d-331e-4e12-b6b2-7e1ad2519acc`.
- POST returned HTTP 202/queued; the claimed job reported HTTP 200/running.
- Final detail and status returned HTTP 200/`insufficient_data`, stage `optimization_review`, five days and INR 40,058.00 known subtotal.
- Geoapify, Open-Meteo, Tavily, Gemini and OpenRouteService calls succeeded. No provider response was mocked in this replay.
- A fresh process retrieved the same saved result.
- Interrupted-job proof ID: `e1620add-c342-46b4-b4c5-d9db13e2b430`. One process exited after claim; after the natural 45-second lease expiry, another process claimed, ran all six real agents and persisted the same partial budget/five-day plan.
- Replay and restart proof files are under ignored `.pytest-tmp/` storage. They use isolated SQLite and explicitly disable production PostgreSQL and Redis writes.

## I. Remaining limitations

- Amadeus is not configured, so no live flight price is available. There is no live hotel inventory/pricing adapter.
- Registry coverage is reviewed but limited to supported Japan, India and Dubai scopes. It is not worldwide market pricing and needs periodic source review.
- A country-only origin cannot support route pricing until the traveler selects a departure city/airport. A broad destination is resolved only with reliable provider evidence; otherwise the API asks for clarification.
- Live FX connectivity was not exercised by the same-currency real replay.
- PostgreSQL-specific concurrency/recovery tests remain skipped until a dedicated test database is provided. SQLite process recovery does not prove PostgreSQL lock behavior on Render.
- No browser automation was run; React rendering is covered by Vitest using a captured real response.
- The repository has no commits and no GitHub remote. Nothing was committed, pushed or deployed.
- API authentication, rate limiting, worker monitoring and production deployment remain separate deployment work.

## J. Final verdict

**PARTIALLY FIXED**

The reproduced all-unpriced and volatile-worker failures are repaired and verified without fabricated prices. A complete booking-ready result cannot be claimed because live transport/hotel pricing is not configured and PostgreSQL-specific worker concurrency still needs a dedicated test database.
