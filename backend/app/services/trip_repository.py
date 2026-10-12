from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.orm import Session

from ..database.models import (
    AgentRun,
    ItineraryActivity,
    ItineraryDay,
    TravelPreference,
    Trip,
)
from ..schemas.travel import (
    AgentRunCreate,
    ItineraryActivityCreate,
    ItineraryDayCreate,
    TravelPreferenceCreate,
    TripCreate,
)


class TripRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_preference(
        self, data: TravelPreferenceCreate
    ) -> TravelPreference:
        return self._save(TravelPreference(**data.model_dump()))

    def get_preference(self, preference_id: UUID) -> TravelPreference | None:
        return self.session.get(TravelPreference, preference_id)

    def create_trip(self, data: TripCreate) -> Trip:
        return self._save(Trip(**data.model_dump()))

    def get_trip(self, trip_id: UUID) -> Trip | None:
        return self.session.get(Trip, trip_id)

    def save_itinerary_days(
        self, trip_id: UUID, days: Sequence[ItineraryDayCreate]
    ) -> list[ItineraryDay]:
        records = [
            ItineraryDay(trip_id=trip_id, **day.model_dump()) for day in days
        ]
        return self._save_many(records)

    def save_activities(
        self,
        itinerary_day_id: UUID,
        activities: Sequence[ItineraryActivityCreate],
    ) -> list[ItineraryActivity]:
        records: list[ItineraryActivity] = []
        for activity in activities:
            values = activity.model_dump()
            values["metadata_json"] = values.pop("metadata")
            records.append(
                ItineraryActivity(itinerary_day_id=itinerary_day_id, **values)
            )
        return self._save_many(records)

    def save_agent_run(
        self, trip_id: UUID, data: AgentRunCreate
    ) -> AgentRun:
        values = data.model_dump(exclude_none=True)
        return self._save(AgentRun(trip_id=trip_id, **values))

    def _save[T](self, record: T) -> T:
        try:
            self.session.add(record)
            self.session.commit()
            self.session.refresh(record)
            return record
        except Exception:
            self.session.rollback()
            raise

    def _save_many[T](self, records: list[T]) -> list[T]:
        if not records:
            return []
        try:
            self.session.add_all(records)
            self.session.commit()
            for record in records:
                self.session.refresh(record)
            return records
        except Exception:
            self.session.rollback()
            raise
