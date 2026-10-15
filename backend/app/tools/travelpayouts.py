"""Optional Aviasales cached references; no inferred passenger basis or fares."""
from datetime import UTC, date, datetime

from pydantic import ValidationError

from ..schemas.budget import BudgetLineItem, PriceSource
from ..schemas.destination_research import SourceReference
from ..schemas.flight_pricing import CachedFlightReference, FlightPricingScope, PassengerPricingBasis
from .base import TravelAPITool, TravelAPIResponseError
from .travel_pricing import location_key

FARES_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
PLACES_URL = "https://autocomplete.travelpayouts.com/places2"


def _flight_date(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Invalid date.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def _timestamp(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Invalid timestamp.")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class TravelpayoutsPricingTool(TravelAPITool):
    provider = "travelpayouts"

    def __init__(self, *args, passenger_basis: PassengerPricingBasis | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        # Trusted adapter-level contract evidence, never a user input or an
        # inferred field from a search link. Standard API data leaves it unknown.
        self.passenger_basis = passenger_basis or PassengerPricingBasis()
        self.last_references = []

    async def resolve_iata(self, location, *, country_code=None):
        if not isinstance(location, str) or not location.strip():
            return None
        name, _, qualifier = location.partition(",")
        payload = await self.request_json("GET", PLACES_URL, allow_list=True,
            params={"term": name.strip(), "locale": "en", "types[]": ["city", "airport"]})
        if not isinstance(payload, list):
            raise TravelAPIResponseError(self.provider, "Invalid IATA location response.")
        candidates = set()
        for row in payload:
            if not isinstance(row, dict) or row.get("type") not in {"city", "airport"}:
                continue
            code = row.get("code")
            if not isinstance(code, str) or len(code) != 3 or not code.isascii() or not code.isalpha() or not code.isupper():
                continue
            if location_key(str(row.get("name", ""))) != location_key(name) and code != name.strip().upper():
                continue
            if country_code and str(row.get("country_code", "")).casefold() != country_code.casefold():
                continue
            if qualifier and not country_code and qualifier.strip().casefold() not in {
                str(row.get("country_code", "")).casefold(), str(row.get("country_name", "")).casefold(),
            }:
                continue
            candidates.add(code)
        return next(iter(candidates)) if len(candidates) == 1 else None

    async def fare_references(self, requirements, destination, *, country_code=None):
        self.last_references = []
        if not self.settings.TRAVELPAYOUTS_ENABLED:
            return []
        # Check before any request, including public location lookup.
        token = self.api_key("TRAVELPAYOUTS_API_TOKEN")
        if not all((requirements.origin, requirements.destination, requirements.start_date,
                    requirements.end_date, requirements.currency, requirements.travelers_count)):
            return []
        origin = await self.resolve_iata(requirements.origin)
        target = await self.resolve_iata(destination, country_code=country_code)
        if not origin or not target or origin == target:
            return []
        scope = FlightPricingScope(origin_iata=origin, destination_iata=target,
            origin_location=requirements.origin, destination_location=requirements.destination,
            departure_date=requirements.start_date, return_date=requirements.end_date,
            currency=requirements.currency, travelers=requirements.travelers_count)
        payload = await self.request_json("GET", FARES_URL, decimal_json=True,
            headers={"X-Access-Token": token}, params={
                "origin": origin, "destination": target, "departure_at": requirements.start_date.isoformat(),
                "return_at": requirements.end_date.isoformat(), "currency": requirements.currency.lower(),
                "one_way": "false", "sorting": "price", "limit": 30,
            })
        if payload.get("success") is not True or not isinstance(payload.get("data"), list):
            raise TravelAPIResponseError(self.provider, "Invalid cached fare response.")
        retrieved = datetime.now(UTC)
        references = []
        for row in payload["data"]:
            if not isinstance(row, dict):
                continue
            try:
                currency = row.get("currency", payload.get("currency"))
                reference = CachedFlightReference(
                    origin_iata=row.get("origin"), destination_iata=row.get("destination"),
                    departure_date=_flight_date(row.get("departure_at")), return_date=_flight_date(row.get("return_at")),
                    currency=currency.upper() if isinstance(currency, str) else None, price=row.get("price"),
                    passenger_basis=self.passenger_basis, retrieved_at=retrieved,
                    found_at=_timestamp(row.get("found_at")), expires_at=_timestamp(row.get("expires_at")),
                    requested_scope=scope,
                )
                reference.applicability = reference.rejection_reason(requirements) or "applicable"
                references.append(reference)
            except (ValidationError, ValueError, TypeError):
                # Never expose raw payloads, links, or validation exception bodies.
                continue
        self.last_references = references
        return references

    async def flight_cost(self, requirements, destination, *, country_code=None):
        references = await self.fare_references(requirements, destination, country_code=country_code)
        applicable = [(reference.trip_amount(requirements), reference) for reference in references
                      if reference.rejection_reason(requirements) is None]
        if not applicable:
            return None
        amount, reference = min(applicable, key=lambda pair: pair[0])
        basis = reference.passenger_basis
        return BudgetLineItem(category="intercity_transport", description="Cached round-trip flight planning reference",
            amount=amount, currency=reference.currency, classification="estimated", source="reference_data", confidence="low",
            flight_reference=reference,
            pricing_basis=f"Aviasales cached reference for {reference.origin_iata} → {reference.destination_iata}, {reference.departure_date} to {reference.return_date}; {basis.kind}, {basis.priced_passengers} priced passenger(s); requested travelers: {requirements.travelers_count}.",
            assumptions=["Cached search-history price; not live availability or a guaranteed bookable fare.",
                         "All travelers are assumed adults when applying an established per-adult basis. Reconfirm passenger types, fares, taxes and baggage before booking."],
            source_references=[SourceReference(provider="travelpayouts", title="Aviasales cached flight data", url=FARES_URL)],
            price_sources=[PriceSource(name="Travelpayouts Aviasales cached reference", url=FARES_URL,
                                       observed_on=reference.retrieved_at.date(), currency=reference.currency)])
