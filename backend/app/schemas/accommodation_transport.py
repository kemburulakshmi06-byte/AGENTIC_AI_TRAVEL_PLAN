from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field

from .agent_schemas import AgentSchema
from .destination_research import SourceReference

AccommodationType = Literal[
    "budget_hotel",
    "mid_range_hotel",
    "premium_hotel",
    "hostel",
    "homestay",
    "family_accommodation",
]
TransportMode = Literal[
    "driving",
    "walking",
    "cycling",
    "local_transit",
    "flight",
    "train",
    "bus",
]
CostClassification = Literal["estimate", "unknown"]


class CostEstimate(AgentSchema):
    amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    currency: str
    classification: CostClassification
    pricing_basis: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    uncertainty_notes: list[str] = Field(default_factory=list)


class AccommodationRecommendation(AgentSchema):
    recommended_types: list[AccommodationType] = Field(default_factory=list)
    stay_area_suggestions: list[str] = Field(default_factory=list)
    room_count: int | None = Field(default=None, ge=0)
    nights: int | None = Field(default=None, ge=0)
    assumed_occupancy_per_room: int = Field(ge=1)
    estimated_nightly_rate: CostEstimate
    estimated_total_cost: CostEstimate
    live_availability_checked: Literal[False] = False
    assumptions: list[str] = Field(default_factory=list)


class RouteEstimate(AgentSchema):
    provider: Literal["openrouteservice"] = "openrouteservice"
    mode: Literal["driving", "walking", "cycling"]
    distance_kilometers: Decimal = Field(ge=0, max_digits=14, decimal_places=3)
    duration_minutes: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    origin_coordinates: tuple[float, float]
    destination_coordinates: tuple[float, float]
    verified_provider_fact: Literal[True] = True


class TransportOption(AgentSchema):
    mode: TransportMode
    recommendation_reason: str
    supported_by_routing_provider: bool
    live_availability_checked: Literal[False] = False
    route_estimate: RouteEstimate | None = None
    estimated_cost: CostEstimate


class RecommendedTransportMode(AgentSchema):
    mode: TransportMode
    reason: str


class AccommodationTransportRecommendation(AgentSchema):
    accommodation_types: list[AccommodationType] = Field(default_factory=list)
    stay_area_suggestions: list[str] = Field(default_factory=list)
    transport_modes: list[RecommendedTransportMode] = Field(default_factory=list)


class AccommodationTransportPlan(AgentSchema):
    destination_name: str | None = None
    origin_resolution_status: Literal["verified_city", "broad", "unavailable"] = "unavailable"
    currency: str
    source_references: list[SourceReference] = Field(default_factory=list)
    number_of_nights: int | None = Field(default=None, ge=0)
    accommodation: AccommodationRecommendation
    transport_options: list[TransportOption] = Field(default_factory=list)
    accommodation_cost: CostEstimate
    local_transport_cost: CostEstimate
    intercity_transport_cost: CostEstimate
    combined_estimated_cost: CostEstimate
    unpriced_components: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
