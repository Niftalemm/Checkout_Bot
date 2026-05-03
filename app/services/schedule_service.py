from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import CheckoutSession, ScheduledCheckout
from app.schemas.session import (
    ScheduledCheckoutCreateRequest,
    ScheduledCheckoutResponse,
    ScheduledCheckoutStartRequest,
    ScheduledCheckoutStartResponse,
    ScheduledCheckoutUpdateRequest,
)
from app.services.checkout_service import FORM_FILL_NOT_REQUESTED, STATUS_ACTIVE, STATUS_PENDING_DETAILS, ServiceError

CENTRAL_TZ = ZoneInfo("America/Chicago")
NONTERMINAL_SCHEDULE_STATUSES = {"scheduled", "ready", "started"}
TERMINAL_SCHEDULE_STATUSES = {"completed", "canceled"}


class ScheduleService:
    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def parse_central_datetime(checkout_date: str, checkout_time: str) -> datetime:
        try:
            naive = datetime.strptime(f"{checkout_date} {checkout_time}", "%Y-%m-%d %H:%M")
        except ValueError as exc:
            raise ServiceError(
                "Date must use YYYY-MM-DD and time must use 24-hour HH:MM in Central time.",
                status_code=400,
            ) from exc
        central_dt = naive.replace(tzinfo=CENTRAL_TZ)
        now = datetime.now(CENTRAL_TZ)
        if central_dt <= now:
            raise ServiceError("Scheduled checkout time must be in the future.", status_code=400)
        return central_dt.astimezone(UTC)

    @staticmethod
    def to_central(checkout_time: datetime) -> datetime:
        if checkout_time.tzinfo is None:
            checkout_time = checkout_time.replace(tzinfo=UTC)
        return checkout_time.astimezone(CENTRAL_TZ)

    def _base_schedule_query(self):
        return select(ScheduledCheckout).order_by(ScheduledCheckout.checkout_time.asc())

    def _get_schedule(self, schedule_id: str) -> ScheduledCheckout:
        schedule = self.db.get(ScheduledCheckout, schedule_id)
        if not schedule:
            raise ServiceError("Scheduled checkout not found.", status_code=404)
        return schedule

    def _require_owner(self, schedule: ScheduledCheckout, user_id: str) -> None:
        if schedule.creator_discord_user_id != str(user_id):
            raise ServiceError("You do not have access to that scheduled checkout.", status_code=403)

    def create_scheduled_checkout(self, payload: ScheduledCheckoutCreateRequest) -> ScheduledCheckout:
        checkout_time = self.parse_central_datetime(payload.checkout_date, payload.checkout_time)
        schedule = ScheduledCheckout(
            resident_name=payload.resident_name,
            tech_id=payload.tech_id,
            room_number=payload.room_number,
            hall=payload.hall,
            room_side=payload.room_side,
            checkout_time=checkout_time.replace(tzinfo=None),
            timezone="America/Chicago",
            creator_discord_user_id=payload.creator_discord_user_id,
            creator_display_name=payload.creator_display_name,
            discord_channel_id=payload.discord_channel_id,
            status="scheduled",
        )
        self.db.add(schedule)
        self.db.commit()
        self.db.refresh(schedule)
        return schedule

    def list_scheduled_checkouts(
        self,
        user_id: str,
        include_terminal: bool = False,
    ) -> list[ScheduledCheckout]:
        statuses = None if include_terminal else NONTERMINAL_SCHEDULE_STATUSES
        query = self._base_schedule_query().where(
            ScheduledCheckout.creator_discord_user_id == str(user_id)
        )
        if statuses is not None:
            query = query.where(ScheduledCheckout.status.in_(tuple(statuses)))
        return self.db.execute(query).scalars().all()

    def get_scheduled_checkout(self, schedule_id: str, user_id: str) -> ScheduledCheckout:
        schedule = self._get_schedule(schedule_id)
        self._require_owner(schedule, user_id)
        return schedule

    def update_scheduled_checkout(
        self,
        schedule_id: str,
        payload: ScheduledCheckoutUpdateRequest,
    ) -> ScheduledCheckout:
        schedule = self._get_schedule(schedule_id)
        self._require_owner(schedule, payload.creator_discord_user_id)
        if schedule.status in {"started", "completed", "canceled"}:
            raise ServiceError("That scheduled checkout can no longer be edited.", status_code=409)

        updates = payload.model_dump(exclude_none=True)
        checkout_date = updates.pop("checkout_date", None)
        checkout_time = updates.pop("checkout_time", None)
        updates.pop("creator_discord_user_id", None)
        if checkout_date is not None or checkout_time is not None:
            effective_date = checkout_date or self.to_central(schedule.checkout_time).strftime("%Y-%m-%d")
            effective_time = checkout_time or self.to_central(schedule.checkout_time).strftime("%H:%M")
            schedule.checkout_time = self.parse_central_datetime(effective_date, effective_time).replace(tzinfo=None)

        for key, value in updates.items():
            setattr(schedule, key, value)

        schedule.status = "scheduled"
        schedule.reminder_30_sent = False
        schedule.reminder_10_sent = False
        schedule.reminder_at_time_sent = False
        schedule.ready_to_start_notified = False
        schedule.started_session_id = None
        schedule.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(schedule)
        return schedule

    def cancel_scheduled_checkout(self, schedule_id: str, user_id: str) -> ScheduledCheckout:
        schedule = self._get_schedule(schedule_id)
        self._require_owner(schedule, user_id)
        if schedule.status in TERMINAL_SCHEDULE_STATUSES:
            raise ServiceError("That scheduled checkout is already closed.", status_code=409)
        schedule.status = "canceled"
        schedule.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(schedule)
        return schedule

    def _get_channel_active_session(self, channel_id: str) -> CheckoutSession | None:
        return (
            self.db.execute(
                select(CheckoutSession)
                .where(
                    CheckoutSession.channel_id == str(channel_id),
                    CheckoutSession.status.in_([STATUS_PENDING_DETAILS, STATUS_ACTIVE]),
                )
                .order_by(CheckoutSession.created_at.desc())
            )
            .scalars()
            .first()
        )

    def start_scheduled_checkout(
        self,
        schedule_id: str,
        payload: ScheduledCheckoutStartRequest,
    ) -> ScheduledCheckoutStartResponse:
        schedule = self._get_schedule(schedule_id)
        self._require_owner(schedule, payload.creator_discord_user_id)
        if schedule.status in TERMINAL_SCHEDULE_STATUSES:
            raise ServiceError("That scheduled checkout is no longer available.", status_code=409)
        if schedule.status == "started":
            raise ServiceError("That scheduled checkout has already been started.", status_code=409)

        active_session = self._get_channel_active_session(payload.discord_channel_id)
        if active_session is not None:
            if schedule.status != "started" and schedule.checkout_time <= datetime.now(UTC).replace(tzinfo=None):
                schedule.status = "ready"
                schedule.ready_to_start_notified = False
                schedule.updated_at = datetime.utcnow()
                self.db.commit()
                self.db.refresh(schedule)
            return ScheduledCheckoutStartResponse(
                status="blocked",
                message="There is already an active checkout in this channel. Finish it first, then start this scheduled checkout.",
                schedule=ScheduledCheckoutResponse.model_validate(schedule),
                session=None,
            )

        session = CheckoutSession(
            resident_name=schedule.resident_name,
            room_number=schedule.room_number,
            tech_id=schedule.tech_id,
            hall=schedule.hall,
            staff_name=payload.creator_display_name or schedule.creator_display_name,
            room_side=schedule.room_side,
            status=STATUS_ACTIVE,
            started_by=payload.creator_discord_user_id,
            channel_id=payload.discord_channel_id,
            source="discord_schedule",
            form_fill_status=FORM_FILL_NOT_REQUESTED,
            draft_saved=False,
            scheduled_checkout_id=schedule.id,
        )
        self.db.add(session)
        self.db.flush()
        schedule.status = "started"
        schedule.started_session_id = session.id
        schedule.ready_to_start_notified = True
        schedule.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(schedule)
        self.db.refresh(session)
        return ScheduledCheckoutStartResponse(
            status="started",
            message="Scheduled checkout started.",
            schedule=ScheduledCheckoutResponse.model_validate(schedule),
            session=session,
        )

    def get_next_ready_checkout(
        self,
        user_id: str,
        channel_id: str,
        only_unnotified: bool = False,
    ) -> ScheduledCheckout | None:
        query = self._base_schedule_query().where(
            ScheduledCheckout.creator_discord_user_id == str(user_id),
            ScheduledCheckout.discord_channel_id == str(channel_id),
            ScheduledCheckout.status == "ready",
        )
        if only_unnotified:
            query = query.where(ScheduledCheckout.ready_to_start_notified.is_(False))
        return self.db.execute(query).scalars().first()

    def mark_ready_to_start_notified(self, schedule_id: str) -> ScheduledCheckout:
        schedule = self._get_schedule(schedule_id)
        schedule.ready_to_start_notified = True
        schedule.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(schedule)
        return schedule

    def mark_reminder_sent(self, schedule_id: str, reminder_field: str) -> ScheduledCheckout:
        schedule = self._get_schedule(schedule_id)
        if reminder_field not in {"reminder_30_sent", "reminder_10_sent", "reminder_at_time_sent"}:
            raise ServiceError("Unknown reminder flag.", status_code=400)
        setattr(schedule, reminder_field, True)
        if reminder_field == "reminder_at_time_sent" and schedule.status == "scheduled":
            schedule.status = "ready"
        schedule.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(schedule)
        return schedule

    def list_nonterminal_schedules(self) -> list[ScheduledCheckout]:
        return self.db.execute(
            self._base_schedule_query().where(ScheduledCheckout.status.in_(tuple(NONTERMINAL_SCHEDULE_STATUSES)))
        ).scalars().all()

    def list_due_for_catchup(self, now_utc: datetime) -> list[ScheduledCheckout]:
        naive_now = now_utc.astimezone(UTC).replace(tzinfo=None)
        return self.db.execute(
            self._base_schedule_query().where(
                ScheduledCheckout.status.in_(("scheduled", "ready")),
                ScheduledCheckout.checkout_time <= naive_now,
            )
        ).scalars().all()

    def list_due_reminders(self, now_utc: datetime) -> list[tuple[ScheduledCheckout, str, bool]]:
        schedules = self.db.execute(
            self._base_schedule_query().where(ScheduledCheckout.status.in_(("scheduled", "ready")))
        ).scalars().all()
        due: list[tuple[ScheduledCheckout, str, bool]] = []
        for schedule in schedules:
            checkout_at = schedule.checkout_time
            if checkout_at.tzinfo is None:
                checkout_at = checkout_at.replace(tzinfo=UTC)
            now = now_utc.astimezone(UTC)
            if schedule.reminder_at_time_sent:
                continue
            if schedule.reminder_10_sent and now >= checkout_at - timedelta(minutes=10):
                reminder_fields = [("reminder_at_time_sent", checkout_at)]
            elif schedule.reminder_30_sent and now >= checkout_at - timedelta(minutes=30):
                reminder_fields = [
                    ("reminder_at_time_sent", checkout_at),
                    ("reminder_10_sent", checkout_at - timedelta(minutes=10)),
                ]
            else:
                reminder_fields = [
                    ("reminder_at_time_sent", checkout_at),
                    ("reminder_10_sent", checkout_at - timedelta(minutes=10)),
                    ("reminder_30_sent", checkout_at - timedelta(minutes=30)),
                ]
            for field, run_at in reminder_fields:
                if not getattr(schedule, field) and run_at <= now:
                    due.append((schedule, field, self.active_session_exists_for_schedule(schedule)))
                    break
        return due

    def active_session_exists_for_schedule(self, schedule: ScheduledCheckout) -> bool:
        active = self._get_channel_active_session(schedule.discord_channel_id)
        if not active:
            return False
        return active.started_by == schedule.creator_discord_user_id

    def mark_linked_schedule_completed(self, session_id: int) -> ScheduledCheckout | None:
        schedule = (
            self.db.execute(
                select(ScheduledCheckout).where(ScheduledCheckout.started_session_id == session_id)
            )
            .scalars()
            .first()
        )
        if not schedule:
            return None
        schedule.status = "completed"
        schedule.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(schedule)
        return schedule

    def mark_linked_schedule_canceled(self, session_id: int) -> ScheduledCheckout | None:
        schedule = (
            self.db.execute(
                select(ScheduledCheckout).where(ScheduledCheckout.started_session_id == session_id)
            )
            .scalars()
            .first()
        )
        if not schedule:
            return None
        schedule.status = "canceled"
        schedule.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(schedule)
        return schedule
