from __future__ import annotations

from typing import Literal
from datetime import date
from decimal import Decimal

from pydantic import Field, HttpUrl, model_validator

from .agent_schemas import AgentSchema
from .external import DailyWeather


class SourceReference(AgentSchema):
    provider: Literal["tavily", "amadeus"] = "tavily"
    title: str
    url: HttpUrl
    topic: Literal[
        "attractions",
        "history",
        "food",
        "travel_tips",
        "seasonal",
        "general",
    ] = "general"
    content_is_untrusted: Literal[True] = True


class Attraction(AgentSchema):
    place_id: str | None = None
    name: str
    categories: list[str] = Field(default_factory=list)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    distance_meters: float | None = Field(default=None, ge=0)
    formatted_address: str | None = None
    provider: Literal["geoapify"] = "geoapify"
    verified_provider_fact: Literal[True] = True


class WeatherSummary(AgentSchema):
    provider: Literal["open-meteo"] = "open-meteo"
    forecast_available: bool
    weather_status: Literal["available", "unavailable"] = "unavailable"
    daily_forecasts: list[DailyWeather] = Field(default_factory=list)
    limitation: str | None = None

    @model_validator(mode="after")
    def set_weather_status(self) -> WeatherSummary:
        object.__setattr__(
            self,
            "weather_status",
            "available" if self.forecast_available else "unavailable",
        )
        return self


class TravelAdvisory(AgentSchema):
    title: str
    summary: str
    source_urls: list[HttpUrl] = Field(default_factory=list)
    evidence_type: Literal["provider_search_result", "ai_suggestion"]
    content_is_untrusted: bool = True


class RecommendedActivity(AgentSchema):
    title: str
    description: str
    related_place_ids: list[str] = Field(default_factory=list)
    source_urls: list[HttpUrl] = Field(default_factory=list)
    evidence_type: Literal["ai_suggestion"] = "ai_suggestion"


class IntercityTransfer(AgentSchema):
    origin: str
    destination: str
    provider: Literal["openrouteservice"] = "openrouteservice"
    profile: Literal["driving", "walking", "cycling"]
    duration_minutes: Decimal = Field(gt=0)
    distance_kilometers: Decimal = Field(ge=0)
    origin_coordinates: tuple[float, float]
    destination_coordinates: tuple[float, float]


class CityResearchSegment(AgentSchema):
    location_name: str
    start_date: date
    end_date: date
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    attractions: list[Attraction] = Field(default_factory=list)
    weather_summary: WeatherSummary | None = None
    transfer_from_previous: IntercityTransfer | None = None


class DestinationResearch(AgentSchema):
    resolution_status: Literal[
        "verified", "clarification_required", "provider_unavailable",
        "invalid_coordinates", "no_verified_places", "irrelevant_results", "planning_failure",
    ] | None = None
    destination_name: str | None = None
    country_code: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    summary: str | None = None
    attractions: list[Attraction] = Field(default_factory=list)
    weather_summary: WeatherSummary | None = None
    travel_advisories: list[TravelAdvisory] = Field(default_factory=list)
    recommended_activities: list[RecommendedActivity] = Field(default_factory=list)
    source_references: list[SourceReference] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    location_candidates: list[str] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)
    city_segments: list[CityResearchSegment] = Field(default_factory=list)


class SuggestedActivity(AgentSchema):
    title: str
    description: str
    related_place_ids: list[str] = Field(default_factory=list)


class DestinationResearchNarrative(AgentSchema):
    """AI-authored interpretation; provider identifiers/URLs are not accepted."""

    summary: str = Field(min_length=1, max_length=1200)
    ranked_place_ids: list[str] = Field(default_factory=list, max_length=50)
    recommended_activities: list[SuggestedActivity] = Field(
        default_factory=list, max_length=12
    )
    travel_advisories: list[str] = Field(default_factory=list, max_length=12)
