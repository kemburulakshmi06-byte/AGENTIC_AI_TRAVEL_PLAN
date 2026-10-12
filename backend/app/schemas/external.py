from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class ExternalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @field_validator("place_id", check_fields=False)
    @classmethod
    def validate_place_id(cls, value):
        if value is not None and not value.strip():
            raise ValueError("Provider place ID must not be blank.")
        return value


class GeocodingResult(ExternalResponse):
    provider: Literal["geoapify"] = "geoapify"
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    formatted_address: str
    name: str | None = None
    city: str | None = None
    country: str | None = None
    country_code: str | None = None
    postcode: str | None = None
    result_type: str | None = None
    place_id: str | None = None
    state: str | None = None


class PlaceResult(ExternalResponse):
    provider: Literal["geoapify"] = "geoapify"
    place_id: str | None = None
    name: str
    categories: list[str] = Field(default_factory=list)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    distance_meters: float | None = Field(default=None, ge=0)
    formatted_address: str | None = None


class RouteCoordinate(ExternalResponse):
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)


class RouteResult(ExternalResponse):
    provider: Literal["openrouteservice"] = "openrouteservice"
    profile: Literal["driving", "walking", "cycling"]
    distance_meters: float = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    geometry: list[RouteCoordinate] = Field(default_factory=list)


class DailyWeather(ExternalResponse):
    date: date
    temperature_max_celsius: float | None = None
    temperature_min_celsius: float | None = None
    precipitation_sum_millimeters: float | None = Field(default=None, ge=0)
    weather_code: int | None = None
    condition: str | None = None


class WeatherForecast(ExternalResponse):
    provider: Literal["open-meteo"] = "open-meteo"
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    timezone: str
    days: list[DailyWeather]


class TravelSearchSource(ExternalResponse):
    title: str
    url: HttpUrl
    summary: str
    content_is_untrusted: Literal[True] = True


class TravelSearchResult(ExternalResponse):
    provider: Literal["tavily"] = "tavily"
    query: str
    sources: list[TravelSearchSource]
