"""Opt-in live checks. Never access PostgreSQL or Redis or persist trip data.

Run from backend: python scripts/provider_smoke.py --destination "city, country"
Optional --request-json PATH runs the real six-agent graph on that request too.
The file must contain the normal TripPlanRequest payload, never credentials.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings
from app.schemas.destination_research import DestinationResearchNarrative
from app.schemas.trip_api import TripPlanRequest
from app.services.cache_service import CACHE_MISS_VALUE
from app.services.gemini_service import GeminiService
from app.tools.geoapify import GeoapifyTool
from app.tools.open_meteo import OpenMeteoTool
from app.tools.openrouteservice import OpenRouteServiceTool
from app.tools.tavily import TavilyTool


class NoStorage:
    async def get_cached(self, *args, **kwargs):
        return CACHE_MISS_VALUE

    async def set_cached(self, *args, **kwargs):
        return False

    async def aclose(self):
        pass


def emit(operation, outcome, **values):
    print(json.dumps({"operation": operation, "outcome": outcome, **values}, ensure_ascii=True), flush=True)


async def main(args):
    # Keep third-party HTTP logs (which may contain query API keys) disabled.
    logging.disable(logging.CRITICAL)
    settings = Settings(EXTERNAL_API_MAX_RETRIES=0, GEMINI_MAX_RETRIES=0)
    geo = GeoapifyTool(settings, cache=NoStorage())
    routing = OpenRouteServiceTool(settings, cache=NoStorage())
    weather = OpenMeteoTool(settings, cache=NoStorage())
    search = TavilyTool(settings, cache=NoStorage())
    ai = GeminiService(settings)
    location, places = None, []

    async def check(operation, call):
        try:
            result = await call()
            emit(operation, "success", **result)
        except Exception as error:
            emit(operation, "failed", error_type=type(error).__name__,
                 category=getattr(error, "category", "validation"),
                 http_status=getattr(error, "status_code", None))

    async def geocode():
        nonlocal location
        location = await geo.geocode_destination(args.destination)
        return {"resolved": location is not None, "type": location.result_type if location else None}

    async def nearby():
        nonlocal places
        places = await geo.find_nearby_places(location.latitude, location.longitude, limit=3)
        return {"places": len(places)}

    async def route():
        result = await routing.calculate_route((places[0].latitude, places[0].longitude), (places[1].latitude, places[1].longitude), profile="walking")
        return {"route_found": result is not None, "duration_seconds": result.duration_seconds if result else None}

    async def forecast():
        # Use tomorrow: safely inside the forecast horizon across timezones.
        from datetime import timedelta, timezone
        day = datetime.now(timezone.utc).date() + timedelta(days=1)
        result = await weather.get_weather(location.latitude, location.longitude, day, day)
        return {"forecast_days": len(result.days), "timezone": result.timezone}

    async def research():
        result = await search.search_travel_information(args.destination + " visitor travel tips", max_results=1)
        return {"sources": len(result.sources)}

    async def narrative():
        facts = {"requested_destination": args.destination, "provider_places": [{"place_id": place.place_id, "name": place.name} for place in places]}
        result = await ai.generate_structured(json.dumps(facts), DestinationResearchNarrative,
            system_instruction="Summarize only supplied provider facts. Rank only supplied IDs. Do not invent prices, coordinates, schedules or weather. Keep suggestions concise.", max_output_tokens=1200)
        return {"model": settings.GEMINI_MODEL, "structured_valid": True,
                "ranked_ids_grounded": set(result.ranked_place_ids).issubset({place.place_id for place in places})}

    try:
        await check("geoapify.geocode", geocode)
        if location and location.result_type == "city":
            await check("geoapify.places", nearby)
            await check("open-meteo.forecast", forecast)
        else:
            emit("place/forecast/route", "skipped", reason="Provide a reliably resolved city for a bounded smoke check.")
        if len(places) >= 2:
            await check("openrouteservice.walking", route)
        else:
            emit("openrouteservice.walking", "skipped", reason="Two provider places are required.")
        await check("tavily.search", research)
        await check("gemini.structured", narrative)
        if args.request_json:
            from app.agents.requirements_agent import RequirementsAgent
            from app.agents.destination_research_agent import DestinationResearchAgent
            from app.agents.accommodation_transport_agent import AccommodationTransportAgent
            from app.agents.budget_agent import BudgetAgent
            from app.agents.itinerary_agent import ItineraryAgent
            from app.agents.optimization_agent import OptimizationAgent
            from app.workflows.travel_graph import TravelAgentSet, plan_trip

            async def workflow():
                request = TripPlanRequest.model_validate_json(Path(args.request_json).read_text(encoding="utf-8"))
                agents = TravelAgentSet(
                    requirements=RequirementsAgent(settings=settings, gemini_service=ai),
                    destination_research=DestinationResearchAgent(settings=settings, gemini_service=ai, geoapify=geo, routing=routing, weather=weather, tavily=search),
                    accommodation_transport=AccommodationTransportAgent(settings=settings, gemini_service=ai, geoapify=geo, routing=routing),
                    budget=BudgetAgent(settings=settings, gemini_service=ai),
                    itinerary=ItineraryAgent(settings=settings, gemini_service=ai, routing=routing),
                    optimization_review=OptimizationAgent(settings=settings, gemini_service=ai),
                )
                state = await plan_trip(request, agents=agents, max_revisions=0,
                                        timeout_seconds=settings.TRIP_WORKFLOW_TIMEOUT_SECONDS,
                                        agent_timeout_seconds=settings.TRIP_AGENT_TIMEOUT_SECONDS)
                review = state.get("optimization_review")
                destination_research = state.get("destination_research")
                budget = state.get("budget_breakdown")
                return {"execution_status": state["execution_status"], "agents_executed": [run.agent_name for run in state.get("agent_history", [])],
                        "research_status": destination_research.resolution_status if destination_research else None,
                        "planning_area": destination_research.destination_name if destination_research else None,
                        "verified_places": len(destination_research.attractions) if destination_research else 0,
                        "city_candidates": destination_research.location_candidates if destination_research else [],
                        "errors": state.get("errors", []),
                        "approved": review.approved if review else False, "warnings": list(dict.fromkeys(state.get("warnings", []))),
                        "total": str(budget.estimated_total) if budget and budget.estimated_total is not None else None,
                        "budget": budget.model_dump(mode="json") if budget else None,
                        "amadeus_configured": bool(settings.AMADEUS_API_KEY and settings.AMADEUS_API_SECRET)}
            await check("langgraph.live", workflow)
    finally:
        for tool in (geo, routing, weather, search, ai):
            await tool.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--request-json")
    asyncio.run(main(parser.parse_args()))
