from __future__ import annotations

import asyncio
import re
from math import asin, cos, radians, sin, sqrt
from typing import Any, Literal

from pydantic import ValidationError

from ..schemas.external import GeocodingResult, PlaceResult
from ..core.diagnostics import record_event, safe_failure_reason
from time import perf_counter
from .base import TravelAPIResponseError, TravelAPITool
from .place_interests import INTEREST_CATEGORIES, interest_groups, tokens as _location_tokens

_GEOCODE_URL = "https://api.geoapify.com/v1/geocode/search"
_PLACES_URL = "https://api.geoapify.com/v2/places"
_PLACE_CATEGORIES = {
    "attractions": "tourism.attraction,tourism.sights",
    "hotels": "accommodation.hotel",
    "tourist_places": "tourism.attraction,tourism.sights,entertainment",
    **{group: ",".join(prefix for prefix in categories if prefix != "heritage")
       for group, categories in INTEREST_CATEGORIES.items()},
}
_PLACES_TTL_SECONDS = 86_400
_CITY_DUPLICATE_RADIUS_METERS = 25_000
_REGION_DUPLICATE_RADIUS_METERS = 250_000
_UNKNOWN_DUPLICATE_RADIUS_METERS = 1_000
_ADMINISTRATIVE_TYPE_TOKENS = {
    "county",
    "district",
    "emirate",
    "governorate",
    "prefecture",
    "province",
    "region",
    "state",
    "territory",
}


class GeoapifyTool(TravelAPITool):
    provider = "geoapify"

    async def geocode_destination(
        self, destination: str, *, language: str = "en"
    ) -> GeocodingResult | None:
        query = destination.strip()
        if not query:
            raise ValueError("destination must not be empty.")
        if len(query) > 200:
            raise ValueError("destination must be 200 characters or fewer.")
        normalized_language = language.strip().lower()
        if not normalized_language or len(normalized_language) > 8:
            raise ValueError("language must be a valid short language code.")
        identifier = {"query": query.casefold(), "language": normalized_language}

        async def load() -> GeocodingResult | None:
            payload = await self.request_json(
                "GET",
                _GEOCODE_URL,
                params={
                    "text": query,
                    "lang": normalized_language,
                    "limit": 5,
                    "apiKey": self.api_key("GEOAPIFY_API_KEY"),
                },
            )
            features = payload.get("features")
            if not isinstance(features, list):
                raise TravelAPIResponseError(
                    self.provider, "geocoding response has no feature list."
                )
            if not features:
                return None
            candidates: list[tuple[int, GeocodingResult]] = []
            invalid_count = 0
            coordinate_count = 0
            for feature in features:
                try:
                    result = GeocodingResult.model_validate(
                        _geocode_properties(_feature_properties(feature))
                    )
                except ValidationError as error:
                    invalid_count += 1
                    coordinate_count += any(item["loc"][0] in {"latitude", "longitude"} for item in error.errors())
                    continue
                except TravelAPIResponseError:
                    invalid_count += 1
                    continue
                score = _destination_match_score(query, result)
                if score is not None:
                    candidates.append((score, result))
            if not candidates:
                if invalid_count == len(features):
                    raise TravelAPIResponseError(
                        self.provider, "geocoding results contained no valid locations.",
                        category="invalid_coordinates" if coordinate_count == len(features) else "invalid_schema",
                    )
                raise TravelAPIResponseError(self.provider, "Geocoding results do not match the requested destination.", category="irrelevant_results")
            candidates.sort(key=lambda candidate: candidate[0], reverse=True)
            best_score = candidates[0][0]
            best = [
                result for score, result in candidates if score == best_score
            ]
            locality_groups = _group_same_locality(best)
            if len(locality_groups) > 1:
                return None
            # Geoapify orders equally scored results by provider relevance. Once
            # every best result is proven to represent the same locality, retain
            # the first provider-ranked record instead of treating alternate
            # locality centroids as different destinations.
            return locality_groups[0][0]

        def parse_cached(value):
            # Older cached schemas had no boundary metadata. Optional fields
            # otherwise make those records look valid for the new resolver.
            if isinstance(value, dict) and "result_type" not in value:
                raise ValueError("Legacy geocoding cache requires a fresh provider lookup.")
            result = GeocodingResult.model_validate(value)
            if result.result_type in {"country", "state", "region", "county"} and not (
                result.place_id or (result.result_type == "country" and result.country_code)
            ):
                raise ValueError("Cached broad location has no provider boundary.")
            return result

        return await self.cached(
            "destination",
            identifier,
            parse_cached,
            load,
            ttl_seconds=_PLACES_TTL_SECONDS,
        )

    async def find_destination_cities(
        self, location: GeocodingResult, *, limit: int = 5,
    ) -> list[GeocodingResult]:
        if not 1 <= limit <= 10:
            raise ValueError("City candidate limit must be between 1 and 10.")
        if location.place_id:
            boundary = f"place:{location.place_id}"
        elif location.result_type == "country" and location.country_code:
            boundary = f"countrycode:{location.country_code.lower()}"
        else:
            raise ValueError("A provider boundary is required to discover cities.")

        async def load() -> list[GeocodingResult]:
            payload = await self.request_json("GET", _PLACES_URL, params={
                "categories": "populated_place.city,populated_place.town",
                "filter": boundary, "limit": limit,
                "apiKey": self.api_key("GEOAPIFY_API_KEY"),
            })
            if not isinstance(payload.get("features"), list):
                raise TravelAPIResponseError(self.provider, "City lookup has no feature list.")
            cities, invalid_count, irrelevant_count, coordinate_count = [], 0, 0, 0
            seen = set()
            for feature in payload["features"]:
                try:
                    values = _geocode_properties(_feature_properties(feature))
                    values["result_type"] = "city"
                    city = GeocodingResult.model_validate(values)
                except ValidationError as error:
                    invalid_count += 1
                    coordinate_count += any(item["loc"][0] in {"latitude", "longitude"} for item in error.errors())
                    continue
                except TravelAPIResponseError:
                    invalid_count += 1
                    continue
                if location.country_code and city.country_code and city.country_code.casefold() != location.country_code.casefold():
                    irrelevant_count += 1
                    continue
                identity = (city.latitude, city.longitude)
                if identity not in seen:
                    cities.append(city)
                    seen.add(identity)
            if payload["features"] and not cities:
                category = "invalid_coordinates" if coordinate_count == len(payload["features"]) else (
                    "irrelevant_results" if irrelevant_count else "invalid_schema")
                raise TravelAPIResponseError(self.provider, "City lookup has no valid relevant candidates.", category=category)
            if invalid_count or irrelevant_count:
                record_event(self.provider, "cities.parse", "partial", perf_counter(),
                             category="irrelevant_results" if irrelevant_count else "invalid_schema")
            return cities

        def parse_cached_cities(values):
            cities = [GeocodingResult.model_validate(value) for value in values]
            if any(location.country_code and city.country_code and
                   city.country_code.casefold() != location.country_code.casefold() for city in cities):
                raise ValueError("Cached city candidates do not belong to the requested country.")
            return cities

        return await self.cached(
            "places", {"city_boundary": boundary, "limit": limit},
            parse_cached_cities,
            load, ttl_seconds=_PLACES_TTL_SECONDS,
        )

    async def find_nearby_places(
        self,
        latitude: float,
        longitude: float,
        *,
        category: Literal["attractions", "hotels", "tourist_places", "food", "nature", "culture"] = "attractions",
        radius_meters: int = 5_000,
        limit: int = 20,
    ) -> list[PlaceResult]:
        _validate_coordinates(latitude, longitude)
        if category not in _PLACE_CATEGORIES:
            raise ValueError("Unsupported Geoapify place category.")
        if not 1 <= radius_meters <= 50_000:
            raise ValueError("radius_meters must be between 1 and 50000.")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100.")
        identifier = {
            "latitude": latitude,
            "longitude": longitude,
            "category": category,
            "radius_meters": radius_meters,
            "limit": limit,
        }

        async def load() -> list[PlaceResult]:
            api_key = self.api_key("GEOAPIFY_API_KEY")
            payload = await self.request_json(
                "GET",
                _PLACES_URL,
                params={
                    "categories": _PLACE_CATEGORIES[category],
                    "filter": f"circle:{longitude},{latitude},{radius_meters}",
                    "bias": f"proximity:{longitude},{latitude}",
                    "limit": limit,
                    "apiKey": api_key,
                },
            )
            features = payload.get("features")
            if not isinstance(features, list):
                raise TravelAPIResponseError(
                    self.provider, "places response has no feature list."
                )
            places: list[PlaceResult] = []
            invalid_count = 0
            coordinate_count = 0
            irrelevant_count = 0
            seen = set()
            for feature in features:
                try:
                    properties = _feature_properties(feature)
                    place = PlaceResult.model_validate(_place_properties(properties))
                except ValidationError as error:
                    invalid_count += 1
                    coordinate_count += any(item["loc"][0] in {"latitude", "longitude"} for item in error.errors())
                    continue
                except TravelAPIResponseError:
                    invalid_count += 1
                    continue
                if _distance_meters(latitude, longitude, place.latitude, place.longitude) > radius_meters:
                    irrelevant_count += 1
                    continue
                if "no_access" in place.categories:
                    irrelevant_count += 1
                    continue
                identity = (place.place_id, place.latitude, place.longitude)
                if identity not in seen:
                    places.append(place)
                    seen.add(identity)
            if features and invalid_count == len(features):
                raise TravelAPIResponseError(self.provider, "places response contained no valid features.",
                    category="invalid_coordinates" if coordinate_count == len(features) else "invalid_schema")
            if features and not places and irrelevant_count:
                raise TravelAPIResponseError(self.provider, "Places are outside the requested area or inaccessible.", category="irrelevant_results")
            if invalid_count or irrelevant_count:
                record_event(self.provider, "places.parse", "partial", perf_counter(),
                             category="irrelevant_results" if irrelevant_count else "invalid_schema")
            return places

        return await self.cached(
            "places",
            identifier,
            lambda values: [PlaceResult.model_validate(value) for value in values],
            load,
            ttl_seconds=_PLACES_TTL_SECONDS,
        )

    async def find_interest_places(self, latitude, longitude, *, interests, limit=30, warnings=None):
        categories = ["attractions", *interest_groups(interests)]
        results = await asyncio.gather(*(
            self.find_nearby_places(latitude, longitude, category=category, limit=max(1, limit // len(categories)))
            for category in categories
        ), return_exceptions=True)
        places, errors = [], []
        for result in results:
            if isinstance(result, Exception):
                errors.append(result)
            else:
                places.extend(result)
        if errors and warnings is not None:
            warnings.extend("Geoapify interest research: " + safe_failure_reason(error) + "." for error in errors)
        if errors and not places:
            raise errors[0]
        return places


def _feature_properties(feature: Any) -> dict[str, Any]:
    if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
        raise TravelAPIResponseError(
            "geoapify", "provider returned an invalid feature."
        )
    return feature["properties"]


def _geocode_properties(properties: dict[str, Any]) -> dict[str, Any]:
    try:
        latitude = properties["lat"]
        longitude = properties["lon"]
        formatted = properties.get("formatted") or properties.get("address_line1")
        if not formatted:
            formatted = properties.get("name")
        if not formatted:
            raise KeyError("formatted")
        return {
            "latitude": latitude,
            "longitude": longitude,
            "formatted_address": formatted,
            "name": properties.get("name"),
            "city": properties.get("city") or properties.get("town"),
            "country": properties.get("country"),
            "country_code": properties.get("country_code"),
            "postcode": properties.get("postcode"),
            "result_type": properties.get("result_type"),
            "place_id": properties.get("place_id"),
            "state": properties.get("state"),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise TravelAPIResponseError(
            "geoapify", "geocoding result was missing required location fields."
        ) from exc


def _place_properties(properties: dict[str, Any]) -> dict[str, Any]:
    try:
        datasource = properties.get("datasource")
        raw_data = datasource.get("raw") if isinstance(datasource, dict) else None
        raw_id = raw_data.get("osm_id") if isinstance(raw_data, dict) else None
        place_id = properties.get("place_id") or raw_id
        return {
            "place_id": str(place_id) if place_id is not None else None,
            "name": properties.get("name"),
            "categories": properties.get("categories") or [],
            "latitude": properties["lat"],
            "longitude": properties["lon"],
            "distance_meters": properties.get("distance"),
            "formatted_address": properties.get("formatted"),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise TravelAPIResponseError(
            "geoapify", "place result was missing required fields."
        ) from exc


def _destination_match_score(
    query: str,
    result: GeocodingResult,
) -> int | None:
    query_tokens = _location_tokens(query)
    candidate_tokens = _location_tokens(
        " ".join(
            value
            for value in (
                result.formatted_address,
                result.name,
                result.city,
                result.country,
                result.state,
            )
            if value
        )
    )
    # Accept explicit provider country codes and unambiguous long country-name
    # initialisms (e.g. three-word names). Never derive aliases from city names.
    # Two-letter initialisms are excluded because they can mean a different ISO
    # country; only the provider's actual two-letter code is accepted.
    if result.country_code and re.fullmatch(r"[A-Za-z]{2}", result.country_code):
        candidate_tokens.add(result.country_code.casefold())
    country_words = re.findall(r"[^\W_]+", (result.country or "").casefold())
    initialism = "".join(word[0] for word in country_words if word not in {"of", "the", "and"})
    if len(initialism) >= 3:
        candidate_tokens.add(initialism)
    if not query_tokens or not query_tokens.issubset(candidate_tokens):
        return None
    city_tokens = _location_tokens(result.city or "")
    name_tokens = _location_tokens(result.name or "")
    country_tokens = _location_tokens(result.country or "")
    score = len(query_tokens) * 10
    if city_tokens and city_tokens.issubset(query_tokens):
        score += 100
    if country_tokens and country_tokens.issubset(query_tokens):
        score += 75
    if name_tokens and name_tokens.issubset(query_tokens):
        score += 50
    return score


def _group_same_locality(
    candidates: list[GeocodingResult],
) -> list[list[GeocodingResult]]:
    """Group duplicate provider records without merging distinct places.

    Geoapify can return more than one feature for a city or region, with a
    different administrative/label centroid for each feature. Coordinate
    equality alone therefore produces false ambiguity. Records are duplicates
    only when their locality scope, normalized name, country and available
    region agree, and their centroids are plausible for that scope.
    """
    groups: list[list[GeocodingResult]] = []
    for candidate in candidates:
        for group in groups:
            if all(_same_locality(candidate, member) for member in group):
                group.append(candidate)
                break
        else:
            groups.append([candidate])
    return groups


def _same_locality(first: GeocodingResult, second: GeocodingResult) -> bool:
    first_scope = _location_scope(first)
    second_scope = _location_scope(second)
    if first_scope != second_scope:
        return False

    first_name = _locality_name(first, first_scope)
    second_name = _locality_name(second, second_scope)
    if not first_name or first_name != second_name:
        return False

    first_code = (first.country_code or "").strip().casefold()
    second_code = (second.country_code or "").strip().casefold()
    if first_code and second_code and first_code != second_code:
        return False
    first_country = _normalized_location_value(first.country)
    second_country = _normalized_location_value(second.country)
    if first_country and second_country and first_country != second_country:
        return False
    # At least one comparable country identifier is required before two
    # provider records with different coordinates can be merged.
    if not ((first_code and second_code) or (first_country and second_country)):
        return False

    first_region = _normalized_region_value(first.state)
    second_region = _normalized_region_value(second.state)
    if first_region and second_region and first_region != second_region:
        return False

    if first.place_id and first.place_id == second.place_id:
        return True
    if first_scope == "country":
        return True

    distance = _distance_meters(
        first.latitude,
        first.longitude,
        second.latitude,
        second.longitude,
    )
    if first_scope == "city":
        return distance <= _CITY_DUPLICATE_RADIUS_METERS
    if first_scope == "region":
        return distance <= _REGION_DUPLICATE_RADIUS_METERS
    return distance <= _UNKNOWN_DUPLICATE_RADIUS_METERS


def _location_scope(result: GeocodingResult) -> str:
    result_type = (result.result_type or "").strip().casefold()
    if result_type == "country":
        return "country"
    if result_type in {"state", "region", "county"}:
        return "region"
    if result_type in {
        "city",
        "town",
        "village",
        "municipality",
        "borough",
        "district",
        "suburb",
        "neighbourhood",
        "locality",
    }:
        return "city"
    if result.city:
        return "city"
    if result.state:
        return "region"
    if result.country and _normalized_location_value(result.name) == _normalized_location_value(result.country):
        return "country"
    return "unknown"


def _locality_name(result: GeocodingResult, scope: str) -> tuple[str, ...]:
    if scope == "city":
        return _normalized_location_value(result.city or result.name)
    if scope == "region":
        return _normalized_location_value(result.state or result.name)
    if scope == "country":
        return _normalized_location_value(result.country or result.name)
    return _normalized_location_value(result.name or result.formatted_address)


def _normalized_location_value(value: str | None) -> tuple[str, ...]:
    return tuple(sorted(_location_tokens(value or "")))


def _normalized_region_value(value: str | None) -> tuple[str, ...]:
    tokens = _location_tokens(value or "")
    without_type = tokens - _ADMINISTRATIVE_TYPE_TOKENS
    return tuple(sorted(without_type or tokens))


def _distance_meters(lat1, lon1, lat2, lon2):
    """Geographic relevance filter only; never used as a road duration."""
    delta_lat, delta_lon = radians(lat2 - lat1), radians(lon2 - lon1)
    value = sin(delta_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(delta_lon / 2) ** 2
    return 6_371_000 * 2 * asin(sqrt(min(1.0, max(0.0, value))))


def _validate_coordinates(latitude: float, longitude: float) -> None:
    if not -90 <= latitude <= 90:
        raise ValueError("latitude must be between -90 and 90.")
    if not -180 <= longitude <= 180:
        raise ValueError("longitude must be between -180 and 180.")
