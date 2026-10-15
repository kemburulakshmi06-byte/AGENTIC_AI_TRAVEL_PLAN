"""Sourced allowances, never AI-authored prices. Coverage is explicit and fail-closed."""
from __future__ import annotations

import json
import asyncio
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from time import perf_counter
from typing import Literal
from xml.etree import ElementTree

import httpx

from pydantic import Field, HttpUrl, ValidationError, model_validator

from ..core.config import Settings
from ..core.diagnostics import safe_failure_reason, record_event
from ..schemas.agent_schemas import AgentSchema
from ..schemas.budget import BudgetLineItem, CurrencyConversion, PriceSource
from ..schemas.destination_research import DestinationResearch
from ..tools.base import TravelAPIError, TravelAPIResponseError, TravelAPITool, TravelAPITimeoutError, TravelAPIUnavailableError
from ..tools.travel_pricing import AmadeusPricingTool, location_key, full_location_key
from ..tools.travelpayouts import TravelpayoutsPricingTool
from ..schemas.flight_pricing import CachedFlightReference, invalid_cached_flight_line

CENT = Decimal("0.01")


class ReferenceRate(AgentSchema):
    id: str
    category: Literal["accommodation", "intercity_transport", "local_transport", "food", "activities"]
    country_code: str = Field(pattern=r"^[a-z]{2}$")
    destination_cities: list[str] = Field(default_factory=list)
    origins: list[str] = Field(default_factory=list)
    departure_date: date | None = None
    return_date: date | None = None
    styles: list[str] = Field(min_length=1)
    transport_modes: list[str] = Field(default_factory=list)
    unit: Literal["room_night", "person_day", "person_trip"]
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    minimum: Decimal = Field(ge=0)
    typical: Decimal = Field(ge=0)
    maximum: Decimal = Field(ge=0)
    fixed_per_person_trip: Decimal = Field(default=Decimal("0"), ge=0)
    occupancy: int | None = Field(default=None, ge=1)
    confidence: Literal["low", "medium"]
    source_name: str
    source_url: HttpUrl
    additional_sources: list[PriceSource] = Field(default_factory=list)
    observed_on: date
    valid_until: date
    assumptions: list[str] = Field(min_length=1)
    range_supported: bool = True

    @model_validator(mode="after")
    def validate_scope(self):
        if not self.minimum <= self.typical <= self.maximum or self.valid_until < self.observed_on:
            raise ValueError("Invalid reference range or validity interval.")
        if self.category == "intercity_transport" and (not self.origins or not self.destination_cities):
            raise ValueError("Transport references require exact origin and destination coverage.")
        if (self.departure_date is None) != (self.return_date is None):
            raise ValueError("Travel-date references require both departure and return dates.")
        if self.departure_date is not None and self.category != "intercity_transport":
            raise ValueError("Travel-date scope is supported only for intercity references.")
        if (
            self.departure_date is not None
            and self.return_date is not None
            and self.return_date < self.departure_date
        ):
            raise ValueError("Reference return date cannot precede departure date.")
        if self.unit == "room_night" and self.occupancy is None:
            raise ValueError("Room references must specify supported occupancy.")
        return self


class ReferenceData(AgentSchema):
    version: int = 1
    style_aliases: dict[str, str]
    rates: list[ReferenceRate]


class ReferenceFXTool(TravelAPITool):
    provider = "frankfurter"

    async def conversion(self, base: str, quote: str):
        if base == quote:
            return Decimal("1"), date.today(), None
        started = perf_counter()
        try:
            async with asyncio.timeout(3):
                return await self._frankfurter_conversion(base, quote)
        except (TravelAPIError, TimeoutError) as error:
            record_event("frankfurter", "reference_fx", "failed", started,
                         category="timeout" if isinstance(error, TimeoutError) else error.category)
            # A second independent official feed, not a guessed exchange rate.
            fallback = ECBReferenceFXTool(self.settings, client=self._get_client(), cache=self.cache)
            return await fallback.conversion(base, quote)

    async def _frankfurter_conversion(self, base: str, quote: str):
        url = f"https://api.frankfurter.dev/v2/rate/{base}/{quote}"
        payload = await self.request_json("GET", url, decimal_json=True)
        try:
            rate = Decimal(str(payload["rate"]))
            dated = date.fromisoformat(payload["date"])
            if (payload["base"] != base or payload["quote"] != quote or not rate.is_finite()
                or rate <= 0 or not 0 <= (date.today() - dated).days <= self.settings.REFERENCE_FX_MAX_AGE_DAYS):
                raise ValueError("Invalid reference FX.")
        except (KeyError, TypeError, ValueError, ArithmeticError) as error:
            raise TravelAPIResponseError(self.provider, "Invalid or stale reference FX.") from error
        return rate, dated, url


class ECBReferenceFXTool(TravelAPITool):
    provider = "ecb"
    url = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

    async def conversion(self, base: str, quote: str):
        started = perf_counter()
        try:
            response = await self._get_client().get(self.url, timeout=5)
            if not response.is_success:
                raise TravelAPIResponseError(self.provider, "Reference feed unavailable.", status_code=response.status_code, category="http_error")
            body = response.content
            if len(body) > 100000 or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
                raise ValueError("Invalid XML feed.")
            tree = ElementTree.fromstring(body)
            dated_rows = [node for node in tree.iter() if "time" in node.attrib]
            if len(dated_rows) != 1:
                raise ValueError("Ambiguous rate date.")
            dated = date.fromisoformat(dated_rows[0].attrib["time"])
            rates = {"EUR": Decimal("1")}
            for node in dated_rows[0]:
                code, rate = node.attrib["currency"], Decimal(node.attrib["rate"])
                if code in rates or len(code) != 3 or not code.isupper() or not rate.is_finite() or rate <= 0:
                    raise ValueError("Invalid reference rate.")
                rates[code] = rate
            if not 0 <= (date.today() - dated).days <= self.settings.REFERENCE_FX_MAX_AGE_DAYS:
                raise ValueError("Stale reference rate.")
            result = rates[quote] / rates[base]
        except httpx.TimeoutException as error:
            record_event(self.provider, "reference_fx", "failed", started, category="timeout")
            raise TravelAPITimeoutError(self.provider, "Reference feed timed out.", category="timeout") from error
        except httpx.RequestError as error:
            record_event(self.provider, "reference_fx", "failed", started, category="network")
            raise TravelAPIUnavailableError(self.provider, "Reference feed unreachable.", category="network") from error
        except (ElementTree.ParseError, KeyError, TypeError, ValueError, ArithmeticError) as error:
            record_event(self.provider, "reference_fx", "failed", started, category="invalid_schema")
            raise TravelAPIResponseError(self.provider, "Reference feed was invalid, stale or lacked the currency.") from error
        record_event(self.provider, "reference_fx", "success", started, status_code=response.status_code)
        return result, dated, self.url


class CostEstimationService:
    def __init__(self, settings: Settings, *, fx=None, pricing=None, reference_data=None, travelpayouts_pricing=None):
        self.settings = settings
        self.fx = fx
        self.pricing = pricing
        self.reference_data = reference_data
        self.travelpayouts_pricing = travelpayouts_pricing
        self.flight_references = []

    async def estimate_ai_assumptions(self, request, *, existing_costs=(), gemini_service=None,
                                      remaining_agent_seconds=None):
        """Explicit foundation entry point; estimate_missing never invokes AI."""
        from .ai_cost_estimation_service import AICostEstimationService
        return await AICostEstimationService(self.settings, gemini_service=gemini_service).estimate_missing(
            request, existing_costs=existing_costs, remaining_agent_seconds=remaining_agent_seconds)

    async def estimate_missing(self, requirements, research_data, plan, missing_categories):
        self.flight_references = []
        started = perf_counter()
        warnings, actions, estimates = [], [], {}
        reference_unavailable = False
        if not missing_categories:
            return estimates, warnings, actions
        def missing(category, reason, action):
            actions.append(f"{category}: {action}")
            record_event("cost_estimation", category, "failed", started, category=reason)
        if not self.settings.AUTOMATIC_COST_ESTIMATION_ENABLED:
            for category in missing_categories:
                missing(category, "disabled", "automatic estimation is disabled; enable it or provide an allowance in the trip currency.")
            return estimates, warnings, actions
        if not (requirements.currency and requirements.travelers_count and requirements.start_date and requirements.end_date):
            return estimates, warnings, ["Provide currency, traveler count and both trip dates for automatic estimates."]
        try:
            research = DestinationResearch.model_validate(research_data or {})
        except (ValueError, ValidationError):
            research = DestinationResearch()
            warnings.append("Destination pricing metadata is invalid; provide a verified destination city and country before using geographic references.")
            record_event("cost_estimation", "destination_metadata", "failed", perf_counter(), category="invalid_schema")
        try:
            data = ReferenceData.model_validate(self.reference_data) if self.reference_data is not None else ReferenceData.model_validate_json(
                self.settings.COST_REFERENCE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError, ValidationError):
            data = ReferenceData(style_aliases={}, rates=[])
            reference_unavailable = True
            warnings.append("Cost reference data is unavailable or invalid; no unsupported prices were substituted.")
            record_event("cost_estimation", "reference_data", "failed", perf_counter(), category="invalid_schema")
        if len(research.city_segments) > 1:
            return estimates, warnings, ["Multi-city costs need references covering every city and transfer, or user-provided whole-trip allowances."]
        destination = research.destination_name or requirements.destination or ""
        preferences = requirements.cost_preferences
        mode = (preferences.transport_mode if preferences else None) or requirements.constraints.get("intercity_routing_mode")
        broad_origin = plan is not None and plan.origin_resolution_status == "broad"
        if ("intercity_transport" in missing_categories and mode in {None, "flight"}
            and not (self.settings.AMADEUS_API_KEY and self.settings.AMADEUS_API_SECRET)):
            warnings.append("Live flight search was not attempted: Amadeus credentials are not configured. Independent accommodation, meal, local transport and activity references remain enabled; flight costs require a matching dated route reference or user allowance.")
            record_event("amadeus", "pricing", "unavailable", started, category="not_configured")
        # A requested mode must never be silently replaced by an economy flight reference.
        if ("intercity_transport" in missing_categories and mode in {None, "flight"}
            and not broad_origin
            and self.settings.AMADEUS_API_KEY and self.settings.AMADEUS_API_SECRET):
            tool = self.pricing or AmadeusPricingTool(settings=self.settings)
            try:
                async with asyncio.timeout(min(20, self.settings.TRIP_AGENT_TIMEOUT_SECONDS / 3)):
                    quote = await tool.flight_cost(requirements, destination, country_code=research.country_code)
                if quote is not None:
                    quote = BudgetLineItem.model_validate(quote)
                    if (quote.category != "intercity_transport" or quote.currency != requirements.currency
                        or quote.evidence_kind != "verified_price"):
                        raise ValueError("Unusable provider quote.")
                    estimates["intercity_transport"] = quote
                else:
                    warnings.append("Amadeus returned no usable production quote; sandbox offers are not used as real prices.")
            except (TravelAPIError, ValueError, ValidationError, TypeError, TimeoutError) as error:
                warnings.append("Amadeus pricing was unavailable or invalid; only supported reference allowances may be used.")
                record_event("amadeus", "pricing", "failed", perf_counter(), category="timeout" if isinstance(error, TimeoutError) else getattr(error, "category", "invalid_schema"))
            finally:
                if self.pricing is None:
                    await tool.aclose()
        if ("intercity_transport" in missing_categories and "intercity_transport" not in estimates
            and mode in {None, "flight"} and not broad_origin and self.settings.TRAVELPAYOUTS_ENABLED):
            tool = self.travelpayouts_pricing or TravelpayoutsPricingTool(settings=self.settings)
            try:
                async with asyncio.timeout(min(20, self.settings.TRIP_AGENT_TIMEOUT_SECONDS / 3)):
                    quote = await tool.flight_cost(requirements, destination, country_code=research.country_code)
                self.flight_references = [CachedFlightReference.model_validate(value)
                                          for value in getattr(tool, "last_references", [])]
                if quote is not None:
                    quote = BudgetLineItem.model_validate(quote)
                    if quote.flight_reference is None or invalid_cached_flight_line(quote, requirements):
                        raise ValueError("Inapplicable cached flight reference.")
                    estimates["intercity_transport"] = quote
                    warnings.append("Travelpayouts supplied an applicable cached planning reference; live availability and booking prices were not checked.")
                else:
                    warnings.append("Travelpayouts returned no applicable exact-trip fare with established currency and passenger pricing basis; unrelated dates and unknown passenger totals were not substituted. Supply a flight allowance or confirm an applicable priced source.")
            except (TravelAPIError, ValueError, ValidationError, TypeError, TimeoutError) as error:
                warnings.append("Travelpayouts: " + ("provider request timed out" if isinstance(error, TimeoutError) else safe_failure_reason(error)) + ". Existing references or user allowances may still apply.")
                record_event("travelpayouts", "pricing", "failed", perf_counter(),
                             category="timeout" if isinstance(error, TimeoutError) else getattr(error, "category", "invalid_schema"))
            finally:
                if self.travelpayouts_pricing is None:
                    await tool.aclose()
        style = data.style_aliases.get((requirements.travel_style or "balanced").casefold())
        days = (requirements.end_date - requirements.start_date).days + 1
        nights = days - 1
        rooms = preferences.room_count if preferences else None
        occupancy = plan.accommodation.assumed_occupancy_per_room if plan else 2
        if rooms is None and plan:
            rooms = plan.accommodation.room_count
        if rooms is None:
            rooms = (requirements.travelers_count + (occupancy or 2) - 1) // (occupancy or 2)
        fx = self.fx or ReferenceFXTool(settings=self.settings)
        conversions = {}
        try:
            for category in missing_categories:
                if category in estimates:
                    continue
                if category == "intercity_transport" and broad_origin:
                    missing(category, "broad_origin", "choose a departure city or airport within the origin country/region, or supply a whole-trip transport allowance; no arbitrary departure location was selected.")
                    continue
                candidates = [row for row in data.rates
                    if row.category == category and row.country_code == (research.country_code or "").lower()
                    and style in row.styles and row.observed_on <= date.today() <= row.valid_until
                    and (not row.destination_cities or location_key(destination) in map(location_key, row.destination_cities))
                    and (not row.origins or full_location_key(requirements.origin or "") in map(full_location_key, row.origins))
                    and (row.departure_date is None or (
                        row.departure_date == requirements.start_date
                        and row.return_date == requirements.end_date
                    ))
                    and (not row.transport_modes or (mode or "flight") in row.transport_modes)
                    and (row.occupancy is None or (occupancy or 2) == row.occupancy)]
                # Exact travel dates and routes take precedence over broad guides.
                def specificity(row):
                    return (
                        bool(row.departure_date),
                        bool(row.origins),
                        bool(row.destination_cities),
                    )
                candidates.sort(key=specificity, reverse=True)
                if not candidates or (len(candidates) > 1 and specificity(candidates[0]) == specificity(candidates[1])):
                    country_rows = [row for row in data.rates if row.category == category and row.country_code == (research.country_code or "").lower()]
                    if reference_unavailable:
                        reason, detail = "invalid_reference_data", "the configured reference registry could not be loaded or validated"
                    elif not research.country_code:
                        reason, detail = "missing_country", "verified destination country metadata is missing"
                    elif not country_rows:
                        reason, detail = "unsupported_country", "the configured reference registry has no coverage for this category in the destination country"
                    elif not candidates:
                        reason, detail = "unsupported_scope", "no unexpired reference matches the resolved city, origin route, transport mode, travel style and room occupancy"
                        remaining = country_rows
                        for rule, message, matches in (
                            ("expired_reference", "no currently valid reference is available", lambda row: row.observed_on <= date.today() <= row.valid_until),
                            ("unsupported_style", "the requested travel style has no supported reference", lambda row: style in row.styles),
                            ("unsupported_city", "the resolved destination city has no supported reference", lambda row: not row.destination_cities or location_key(destination) in map(location_key, row.destination_cities)),
                            ("unsupported_route", "the requested origin-to-destination route has no supported reference", lambda row: not row.origins or full_location_key(requirements.origin or "") in map(full_location_key, row.origins)),
                            ("unsupported_travel_dates", "the requested departure and return dates have no supported reference", lambda row: row.departure_date is None or (row.departure_date == requirements.start_date and row.return_date == requirements.end_date)),
                            ("unsupported_transport", "the requested transport mode has no supported reference", lambda row: not row.transport_modes or (mode or "flight") in row.transport_modes),
                            ("unsupported_occupancy", "the requested room occupancy has no supported reference", lambda row: row.occupancy is None or (occupancy or 2) == row.occupancy),
                        ):
                            remaining = [row for row in remaining if matches(row)]
                            if not remaining:
                                reason, detail = rule, message
                                break
                    else:
                        reason, detail = "ambiguous_reference", "multiple equally specific references match; the registry requires review"
                    missing(category, reason, detail + "; supply an allowance or configure a dated reference/provider covering this destination, route and travel style.")
                    continue
                row = candidates[0]
                if category == "accommodation" and nights > 0 and row.occupancy is not None and rooms * row.occupancy < requirements.travelers_count:
                    missing(category, "insufficient_room_capacity", "the selected room count cannot accommodate all travelers under the reference occupancy; confirm room capacity, increase rooms or provide a suitable whole-trip accommodation allowance.")
                    continue
                if category == "activities" and (requirements.constraints.get("must_visit_attraction_ids")
                    or any(word in " ".join(requirements.interests).casefold() for word in ("theme park", "guided tour", "premium"))):
                    missing(category, "unsupported_activity_scope", "required paid attractions need matching ticket references or a user-provided activity allowance.")
                    continue
                multiplier = {"room_night": rooms * nights, "person_day": requirements.travelers_count * days,
                              "person_trip": requirements.travelers_count}[row.unit]
                original = row.typical * multiplier + row.fixed_per_person_trip * requirements.travelers_count
                if row.currency != requirements.currency and not self.settings.REFERENCE_FX_ENABLED:
                    missing(category, "fx_disabled", f"enable reference FX for {row.currency} to {requirements.currency}, or supply an allowance in {requirements.currency}.")
                    continue
                key = (row.currency, requirements.currency)
                if key not in conversions:
                    try:
                        async with asyncio.timeout(min(10, self.settings.TRIP_AGENT_TIMEOUT_SECONDS / 6)):
                            conversions[key] = ((Decimal("1"), date.today(), None) if key[0] == key[1] else await fx.conversion(*key))
                    except (TravelAPIError, ValueError, TypeError, ArithmeticError, TimeoutError) as error:
                        conversions[key] = None
                        warnings.append("Reference FX: " + ("provider request timed out" if isinstance(error, TimeoutError) else safe_failure_reason(error)) + ". Foreign-currency costs remain unknown.")
                conversion = conversions[key]
                if conversion is None:
                    missing(category, "fx_unavailable", f"obtain a dated {row.currency}/{requirements.currency} reference exchange rate or enter an allowance in {requirements.currency}.")
                    continue
                rate, rate_date, url = conversion
                fixed = row.fixed_per_person_trip * requirements.travelers_count
                estimates[category] = BudgetLineItem(
                    category=category, description=category.replace("_", " ").capitalize(),
                    amount=(original * rate).quantize(CENT, rounding=ROUND_HALF_UP), currency=requirements.currency,
                    minimum_amount=((row.minimum * multiplier + fixed) * rate).quantize(CENT, rounding=ROUND_HALF_UP) if row.range_supported else None,
                    maximum_amount=((row.maximum * multiplier + fixed) * rate).quantize(CENT, rounding=ROUND_HALF_UP) if row.range_supported else None,
                    classification="estimated", source="reference_data", confidence=row.confidence,
                    pricing_basis=(
                        f"Reference {row.id}: {row.unit} allowance × {multiplier}; "
                        f"{days} inclusive day(s), {nights} night(s), "
                        f"{requirements.travelers_count} traveler(s), {rooms} room(s)."
                        + (
                            f" Exact travel-date scope: {row.departure_date} to {row.return_date}."
                            if row.departure_date is not None
                            else ""
                        )
                    ),
                    assumptions=[
                        *row.assumptions,
                        (
                            "Scenario bounds are planning allowances, not guaranteed market minimum/maximum or date-specific quotations."
                            if row.range_supported
                            else "No supported uncertainty interval is available; only the sourced point allowance is included."
                        ),
                    ],
                    price_sources=[PriceSource(name=row.source_name, url=row.source_url, observed_on=row.observed_on, currency=row.currency), *row.additional_sources],
                    currency_conversion=(CurrencyConversion(original_currency=row.currency, original_amount=original,
                        target_currency=requirements.currency, rate=rate, rate_date=rate_date,
                        source=("ECB published daily reference FX" if url == ECBReferenceFXTool.url else "Frankfurter central-bank reference FX") + " (not a retail or live trading rate)", source_url=url) if url else None),
                )
                record_event("cost_estimation", category, "success", started)
        finally:
            if self.fx is None:
                await fx.aclose()
        if estimates:
            warnings.append("Automatic reference allowances are non-live planning estimates. Reconfirm fares, hotel availability, tickets and exchange costs before booking; visa, insurance and premium experiences are excluded unless separately supplied.")
        return estimates, warnings, actions
