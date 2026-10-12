"""Optional authorized flight search. Test offers are never booking evidence."""
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

from ..schemas.budget import BudgetLineItem, PriceSource
from ..schemas.destination_research import SourceReference
from .base import TravelAPITool, TravelAPIResponseError


def location_key(value: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", value.split(",")[0].casefold()))


def full_location_key(value: str) -> str:
    """Do not discard an origin's explicit country/region qualification."""
    return " ".join(re.findall(r"[^\W_]+", value.casefold()))


class AmadeusPricingTool(TravelAPITool):
    provider = "amadeus"

    async def flight_cost(self, requirements, destination: str, *, country_code: str | None = None) -> BudgetLineItem | None:
        base = ("https://api.amadeus.com" if self.settings.AMADEUS_ENVIRONMENT == "production"
                else "https://test.api.amadeus.com")
        token = await self.request_json("POST", base + "/v1/security/oauth2/token", form_body={
            "grant_type": "client_credentials", "client_id": self.api_key("AMADEUS_API_KEY"),
            "client_secret": self.api_key("AMADEUS_API_SECRET"),
        })
        access = token.get("access_token")
        if not isinstance(access, str) or not access:
            raise TravelAPIResponseError(self.provider, "Invalid authentication response.")
        headers = {"Authorization": "Bearer " + access}

        async def resolve(name, expected_country=None):
            payload = await self.request_json("GET", base + "/v1/reference-data/locations", headers=headers,
                params={"subType": "CITY,AIRPORT", "keyword": name.split(",")[0], "page[limit]": 10})
            qualifier = name.partition(",")[2].strip().casefold()
            cities = {row.get("iataCode") for row in payload.get("data", [])
                      if isinstance(row, dict) and row.get("subType") == "CITY"
                      and location_key(str(row.get("name", ""))) == location_key(name)
                      and (not expected_country or str((row.get("address") or {}).get("countryCode", "")).casefold() == expected_country.casefold())
                      and (not qualifier or qualifier in {
                          str((row.get("address") or {}).get("countryCode", "")).casefold(),
                          str((row.get("address") or {}).get("countryName", "")).casefold()})
                      and isinstance(row.get("iataCode"), str) and re.fullmatch(r"[A-Z]{3}", row["iataCode"])}
            if len(cities) != 1:
                return None
            city_code = cities.pop()
            airports = {row.get("iataCode") for row in payload.get("data", [])
                        if isinstance(row, dict) and row.get("subType") == "AIRPORT"
                        and isinstance(row.get("address"), dict) and row["address"].get("cityCode") == city_code
                        and isinstance(row.get("iataCode"), str) and re.fullmatch(r"[A-Z]{3}", row["iataCode"])}
            return city_code, airports | {city_code}

        # The resolved base country is already checked by destination research.
        # Origin qualifications still need explicit provider support.
        origin, target = await resolve(requirements.origin), await resolve(destination.split(",")[0], country_code)
        if not origin or not target or origin[0] == target[0]:
            return None
        payload = await self.request_json("GET", base + "/v2/shopping/flight-offers", headers=headers, params={
            "originLocationCode": origin[0], "destinationLocationCode": target[0],
            "departureDate": requirements.start_date.isoformat(), "returnDate": requirements.end_date.isoformat(),
            "adults": requirements.travelers_count, "currencyCode": requirements.currency,
            "travelClass": "ECONOMY", "max": 5,
        })
        if self.settings.AMADEUS_ENVIRONMENT != "production":
            # Sandbox cached offers are neither market quotes nor estimation references.
            return None
        prices = []
        for offer in payload.get("data", []):
            try:
                if len(offer["travelerPricings"]) != requirements.travelers_count or len(offer["itineraries"]) != 2:
                    continue
                out, back = [leg["segments"] for leg in offer["itineraries"]]
                if (out[0]["departure"]["at"][:10] != requirements.start_date.isoformat()
                    or back[0]["departure"]["at"][:10] != requirements.end_date.isoformat()
                    or out[0]["departure"]["iataCode"] not in origin[1]
                    or out[-1]["arrival"]["iataCode"] not in target[1]
                    or back[0]["departure"]["iataCode"] not in target[1]
                    or back[-1]["arrival"]["iataCode"] not in origin[1]
                    or offer["price"]["currency"] != requirements.currency):
                    continue
                amount = Decimal(str(offer["price"]["grandTotal"]))
                if amount.is_finite() and amount > 0:
                    prices.append(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            except (KeyError, IndexError, TypeError, InvalidOperation):
                continue
        if not prices:
            return None
        return BudgetLineItem(
            category="intercity_transport", description="Round-trip economy flights (all travelers)",
            amount=min(prices), currency=requirements.currency, classification="verified", source="verified_provider",
            minimum_amount=min(prices), maximum_amount=max(prices), confidence="medium",
            pricing_basis="Amadeus production search for the requested dates and adult traveler count; cheapest returned offer.",
            assumptions=["All travelers priced as adults. Search quotes require fare and availability reconfirmation before booking.",
                         "Search-result range is not the full market range. Optional baggage and ancillary fees may be additional."],
            source_references=[SourceReference(provider="amadeus", title="Amadeus flight offer search", url=base + "/v2/shopping/flight-offers")],
            price_sources=[PriceSource(name="Amadeus production flight search", url=base + "/v2/shopping/flight-offers",
                                       observed_on=date.today(), currency=requirements.currency)],
        )
