"""Cached flight evidence and deterministic applicability, separate from live fares."""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .agent_schemas import AgentSchema


class PassengerPricingBasis(AgentSchema):
    kind: Literal["unknown", "per_adult", "party_total"] = "unknown"
    priced_passengers: int | None = Field(default=None, ge=1, strict=True)
    evidence: str | None = Field(default=None, min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_basis(self):
        if self.kind != "unknown" and (self.priced_passengers is None or self.evidence is None):
            raise ValueError("Established passenger pricing requires count and evidence.")
        if self.kind == "per_adult" and self.priced_passengers != 1:
            raise ValueError("Per-adult references must price one adult.")
        return self


class FlightPricingScope(AgentSchema):
    origin_iata: str = Field(pattern=r"^[A-Z]{3}$")
    destination_iata: str = Field(pattern=r"^[A-Z]{3}$")
    origin_location: str
    destination_location: str
    departure_date: date
    return_date: date
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    travelers: int = Field(ge=1, strict=True)


class CachedFlightReference(AgentSchema):
    provider: Literal["travelpayouts"] = "travelpayouts"
    classification: Literal["cached_reference"] = "cached_reference"
    live_availability_checked: Literal[False] = False
    origin_iata: str = Field(pattern=r"^[A-Z]{3}$")
    destination_iata: str = Field(pattern=r"^[A-Z]{3}$")
    departure_date: date | None = None
    return_date: date | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    price: Decimal = Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)
    passenger_basis: PassengerPricingBasis = Field(default_factory=PassengerPricingBasis)
    retrieved_at: datetime
    found_at: datetime | None = None
    expires_at: datetime | None = None
    requested_scope: FlightPricingScope
    applicability: str = "not_evaluated"

    @field_validator("retrieved_at", "found_at", "expires_at")
    @classmethod
    def aware_timestamp(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("Freshness timestamps must include timezone.")
        return value

    def rejection_reason(self, request, *, now=None):
        now = now or datetime.now(UTC)
        scope = self.requested_scope
        if (self.origin_iata != scope.origin_iata or self.destination_iata != scope.destination_iata
            or self.origin_iata == self.destination_iata
            or (request.origin or "").strip().casefold() != scope.origin_location.strip().casefold()
            or (request.destination or "").strip().casefold() != scope.destination_location.strip().casefold()):
            return "route_mismatch"
        if (self.departure_date != request.start_date or self.return_date != request.end_date
            or scope.departure_date != request.start_date or scope.return_date != request.end_date
            or self.departure_date is None or self.return_date is None
            or self.return_date < self.departure_date):
            return "date_mismatch"
        if not self.currency or self.currency != request.currency or scope.currency != request.currency:
            return "currency_unknown_or_mismatch"
        if (self.retrieved_at > now or now - self.retrieved_at > timedelta(hours=48)
            or (self.found_at is not None and (self.found_at > self.retrieved_at or now - self.found_at > timedelta(hours=48)))
            or (self.expires_at is not None and self.expires_at <= now)):
            return "stale_reference"
        basis = self.passenger_basis
        if scope.travelers != request.travelers_count or basis.kind == "unknown":
            return "passenger_basis_unknown_or_mismatch"
        if basis.kind == "party_total" and basis.priced_passengers != request.travelers_count:
            return "passenger_basis_unknown_or_mismatch"
        return None

    def trip_amount(self, request):
        if self.rejection_reason(request):
            return None
        multiplier = request.travelers_count if self.passenger_basis.kind == "per_adult" else 1
        return (self.price * multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def invalid_cached_flight_line(item, request):
    """Used again at service, reviewer and workflow boundaries."""
    reference = item.flight_reference
    is_travelpayouts = any(source.provider == "travelpayouts" for source in item.source_references)
    if reference is None:
        return is_travelpayouts
    return bool(reference.rejection_reason(request) or item.category != "intercity_transport"
                or item.currency != request.currency or item.amount != reference.trip_amount(request)
                or item.classification != "estimated" or item.source != "reference_data")
