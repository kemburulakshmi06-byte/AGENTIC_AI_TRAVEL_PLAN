from __future__ import annotations

from typing import Literal

from pydantic import ValidationError

from ..schemas.external import RouteCoordinate, RouteResult
from .base import TravelAPIResponseError, TravelAPITool

_DIRECTIONS_URL = "https://api.openrouteservice.org/v2/directions"
_PROFILES = {
    "driving": "driving-car",
    "walking": "foot-walking",
    "cycling": "cycling-regular",
}


class OpenRouteServiceTool(TravelAPITool):
    provider = "openrouteservice"

    async def calculate_route(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        *,
        profile: Literal["driving", "walking", "cycling"] = "driving",
    ) -> RouteResult | None:
        _validate_coordinate(origin, "origin")
        _validate_coordinate(destination, "destination")
        if profile not in _PROFILES:
            raise ValueError("profile must be driving, walking, or cycling.")
        identifier = {
            "origin": list(origin),
            "destination": list(destination),
            "profile": profile,
        }

        async def load() -> RouteResult | None:
            payload = await self.request_json(
                "POST",
                f"{_DIRECTIONS_URL}/{_PROFILES[profile]}/geojson",
                json_body={
                    # ORS expects [longitude, latitude], not [latitude, longitude].
                    "coordinates": [
                        [origin[1], origin[0]],
                        [destination[1], destination[0]],
                    ]
                },
                headers={
                    "Authorization": self.api_key("OPENROUTESERVICE_API_KEY")
                },
            )
            features = payload.get("features")
            if not isinstance(features, list):
                raise TravelAPIResponseError(
                    self.provider, "directions response has no feature list."
                )
            if not features:
                return None
            feature = features[0]
            if not isinstance(feature, dict):
                raise TravelAPIResponseError(
                    self.provider, "directions response contained an invalid route."
                )
            properties = feature.get("properties")
            summary = properties.get("summary") if isinstance(properties, dict) else None
            if not isinstance(summary, dict):
                raise TravelAPIResponseError(
                    self.provider, "route summary is unavailable."
                )
            geometry_payload = feature.get("geometry")
            if not isinstance(geometry_payload, dict):
                raise TravelAPIResponseError(
                    self.provider, "route geometry is invalid."
                )
            raw_coordinates = geometry_payload.get("coordinates", [])
            if not isinstance(raw_coordinates, list):
                raise TravelAPIResponseError(
                    self.provider, "route geometry is invalid."
                )
            try:
                if any(
                    not isinstance(coordinate, (list, tuple))
                    or len(coordinate) < 2
                    for coordinate in raw_coordinates
                ):
                    raise ValueError("invalid route coordinate")
                geometry = [
                    RouteCoordinate(longitude=coordinate[0], latitude=coordinate[1])
                    for coordinate in raw_coordinates
                ]
                return RouteResult(
                    profile=profile,
                    distance_meters=summary["distance"],
                    duration_seconds=summary["duration"],
                    geometry=geometry,
                )
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                raise TravelAPIResponseError(
                    self.provider, "route response contained invalid measurements."
                ) from exc

        return await self.cached(
            "routes",
            identifier,
            RouteResult.model_validate,
            load,
        )


def _validate_coordinate(coordinate: tuple[float, float], name: str) -> None:
    if len(coordinate) != 2:
        raise ValueError(f"{name} must be (latitude, longitude).")
    latitude, longitude = coordinate
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError(f"{name} latitude/longitude is out of range.")
