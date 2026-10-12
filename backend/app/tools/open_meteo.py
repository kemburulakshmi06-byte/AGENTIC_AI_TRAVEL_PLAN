from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone as utc_timezone
from math import isfinite
from typing import Any

from pydantic import ValidationError

from ..schemas.external import DailyWeather, WeatherForecast
from .base import TravelAPIResponseError, TravelAPITool

_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_FORECAST_DAYS = 16
_WEATHER_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow fall",
    73: "Moderate snow fall",
    75: "Heavy snow fall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


class UnsupportedForecastDateError(ValueError):
    pass


class OpenMeteoTool(TravelAPITool):
    provider = "open-meteo"

    def __init__(
        self,
        *args: Any,
        clock: Callable[[], date] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._today = clock or (lambda: datetime.now(utc_timezone.utc).date())
        self._injected_clock = clock is not None

    async def get_weather(
        self,
        latitude: float,
        longitude: float,
        start_date: date,
        end_date: date,
        *,
        timezone: str = "auto",
    ) -> WeatherForecast:
        """Get daily forecast weather for an inclusive date range."""
        return await self.get_daily_forecast(
            latitude,
            longitude,
            start_date,
            end_date,
            timezone=timezone,
        )

    async def get_daily_forecast(
        self,
        latitude: float,
        longitude: float,
        start_date: date,
        end_date: date | None = None,
        *,
        timezone: str = "auto",
    ) -> WeatherForecast:
        _validate_coordinates(latitude, longitude)
        if not isinstance(start_date, date) or (
            end_date is not None and not isinstance(end_date, date)
        ):
            raise ValueError("start_date and end_date must be date values.")
        final_date = end_date or start_date
        if final_date < start_date:
            raise ValueError("end_date must not precede start_date.")
        today = self._today()
        last_supported_date = today + timedelta(days=_FORECAST_DAYS - 1)
        # With timezone=auto, the provider resolves destination-local dates.
        # Allow the UTC/local day boundary rather than rejecting a valid local
        # date using the backend host's timezone; the provider remains authoritative.
        margin = timedelta(days=0 if self._injected_clock else 1)
        if start_date < today - margin or final_date > last_supported_date + margin:
            raise UnsupportedForecastDateError(
                f"Open-Meteo forecasts are supported from {today.isoformat()} "
                f"through {last_supported_date.isoformat()}; no forecast was fabricated."
            )
        if not timezone.strip():
            raise ValueError("timezone must not be empty.")
        identifier = {
            "latitude": latitude,
            "longitude": longitude,
            "start_date": start_date.isoformat(),
            "end_date": final_date.isoformat(),
            "timezone": timezone,
        }

        async def load() -> WeatherForecast:
            payload = await self.request_json(
                "GET",
                _FORECAST_URL,
                params={
                    "latitude": latitude,
                    "longitude": longitude,
                    "daily": (
                        "temperature_2m_max,temperature_2m_min,"
                        "precipitation_sum,weather_code"
                    ),
                    "temperature_unit": "celsius",
                    "precipitation_unit": "mm",
                    "timezone": timezone,
                    "start_date": start_date.isoformat(),
                    "end_date": final_date.isoformat(),
                },
            )
            daily = payload.get("daily")
            if not isinstance(daily, dict) or not isinstance(daily.get("time"), list):
                raise TravelAPIResponseError(
                    self.provider, "daily forecast response is unavailable."
                )
            times = daily["time"]
            if not times:
                raise TravelAPIResponseError(
                    self.provider, "provider returned no forecast days."
                )
            expected_dates = [(start_date + timedelta(days=index)).isoformat()
                              for index in range((final_date - start_date).days + 1)]
            if times != expected_dates:
                raise TravelAPIResponseError(self.provider, "forecast dates do not match the requested local dates.", category="invalid_schema")
            fields = (
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_sum",
                "weather_code",
            )
            arrays = {field: daily.get(field) for field in fields}
            if any(
                not isinstance(values, list) or len(values) != len(times)
                for values in arrays.values()
            ):
                raise TravelAPIResponseError(
                    self.provider, "daily forecast fields have inconsistent lengths."
                )
            if not any(value is not None for values in arrays.values() for value in values):
                raise TravelAPIResponseError(self.provider, "forecast days contain no weather measurements.", category="empty_response")
            days: list[DailyWeather] = []
            try:
                for index, day_string in enumerate(times):
                    code = _value_at(arrays["weather_code"], index)
                    days.append(
                        DailyWeather(
                            date=date.fromisoformat(day_string),
                            temperature_max_celsius=_value_at(
                                arrays["temperature_2m_max"], index
                            ),
                            temperature_min_celsius=_value_at(
                                arrays["temperature_2m_min"], index
                            ),
                            precipitation_sum_millimeters=_value_at(
                                arrays["precipitation_sum"], index
                            ),
                            weather_code=code,
                            condition=_WEATHER_CODES.get(code) if code is not None else None,
                        )
                    )
                return WeatherForecast(
                    latitude=payload.get("latitude", latitude),
                    longitude=payload.get("longitude", longitude),
                    timezone=payload.get("timezone", timezone),
                    days=days,
                )
            except (TypeError, ValueError, ValidationError) as exc:
                raise TravelAPIResponseError(
                    self.provider, "daily forecast contained invalid values."
                ) from exc

        result = await self.cached(
            "weather",
            identifier,
            WeatherForecast.model_validate,
            load,
        )
        if result is None:
            raise TravelAPIResponseError(
                self.provider, "provider returned no forecast."
            )
        return result


def _value_at(values: Any, index: int) -> Any:
    return values[index] if isinstance(values, list) else None


def _validate_coordinates(latitude: float, longitude: float) -> None:
    if not isfinite(latitude) or not -90 <= latitude <= 90:
        raise ValueError("latitude must be between -90 and 90.")
    if not isfinite(longitude) or not -180 <= longitude <= 180:
        raise ValueError("longitude must be between -180 and 180.")
