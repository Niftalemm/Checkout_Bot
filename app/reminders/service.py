import time
from datetime import UTC, datetime, timedelta

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

from app.core.config import settings
from app.db.base import SessionLocal
from app.models.entities import ScheduledCheckout
from app.services.schedule_service import ScheduleService


def _schedule_components() -> list[dict]:
    return [
        {
            "type": 1,
            "components": [
                {"type": 2, "style": 3, "label": "Start", "custom_id": "schedule_start"},
                {"type": 2, "style": 2, "label": "Edit", "custom_id": "schedule_edit"},
                {"type": 2, "style": 4, "label": "Cancel", "custom_id": "schedule_cancel"},
            ],
        }
    ]


def _format_schedule_message(schedule: ScheduledCheckout, lead: str) -> str:
    central_dt = ScheduleService.to_central(schedule.checkout_time)
    return (
        f"{lead}\n"
        f"Resident: **{schedule.resident_name}**\n"
        f"Room: **{schedule.room_number}** | Hall: **{schedule.hall}** | Side: **{schedule.room_side}**\n"
        f"TechID: **{schedule.tech_id}**\n"
        f"Checkout time: **{central_dt.strftime('%Y-%m-%d %I:%M %p CT')}**\n"
        f"Status: **{schedule.status}**\n"
        f"Schedule ID: `{schedule.id}`"
    )


class ReminderCoordinator:
    def __init__(self):
        self.scheduler = BackgroundScheduler(timezone=UTC)

    def run_forever(self) -> None:
        self.sync_jobs()
        self.scheduler.add_job(self.sync_jobs, "interval", seconds=60, id="scheduled_checkout_sync", replace_existing=True)
        self.scheduler.start()
        print("Scheduled checkout reminder loop started with APScheduler.")
        try:
            while True:
                time.sleep(60)
        finally:
            self.scheduler.shutdown(wait=False)

    def sync_jobs(self) -> None:
        now = datetime.now(UTC)
        with SessionLocal() as db:
            service = ScheduleService(db)
            schedules = service.list_nonterminal_schedules()
        desired_job_ids: set[str] = {"scheduled_checkout_sync"}
        for schedule in schedules:
            desired_job_ids.update(self._schedule_jobs_for_checkout(schedule, now))

        for job in self.scheduler.get_jobs():
            if job.id not in desired_job_ids:
                self.scheduler.remove_job(job.id)
        self.catch_up_missed_reminders()

    def _schedule_jobs_for_checkout(self, schedule: ScheduledCheckout, now: datetime) -> set[str]:
        scheduled_ids: set[str] = set()
        if schedule.status not in {"scheduled", "ready"}:
            return scheduled_ids
        reminder_map = {
            "reminder_30_sent": schedule.checkout_time.replace(tzinfo=UTC) - timedelta(minutes=30),
            "reminder_10_sent": schedule.checkout_time.replace(tzinfo=UTC) - timedelta(minutes=10),
            "reminder_at_time_sent": schedule.checkout_time.replace(tzinfo=UTC),
        }
        for reminder_field, run_at in reminder_map.items():
            if getattr(schedule, reminder_field):
                continue
            if run_at <= now:
                continue
            job_id = f"scheduled_checkout:{schedule.id}:{reminder_field}"
            self.scheduler.add_job(
                self.fire_reminder,
                trigger=DateTrigger(run_date=run_at),
                id=job_id,
                replace_existing=True,
                kwargs={"schedule_id": schedule.id, "reminder_field": reminder_field},
            )
            scheduled_ids.add(job_id)
        return scheduled_ids

    def catch_up_missed_reminders(self) -> None:
        now = datetime.now(UTC)
        with SessionLocal() as db:
            service = ScheduleService(db)
            schedules = service.list_nonterminal_schedules()
        for schedule in schedules:
            if schedule.status not in {"scheduled", "ready"}:
                continue
            checkout_at = schedule.checkout_time.replace(tzinfo=UTC)
            if not schedule.reminder_30_sent and checkout_at - timedelta(minutes=30) <= now:
                self.fire_reminder(schedule.id, "reminder_30_sent")
            if not schedule.reminder_10_sent and checkout_at - timedelta(minutes=10) <= now:
                self.fire_reminder(schedule.id, "reminder_10_sent")
            if not schedule.reminder_at_time_sent and checkout_at <= now:
                self.fire_reminder(schedule.id, "reminder_at_time_sent")

    def fire_reminder(self, schedule_id: str, reminder_field: str) -> None:
        with SessionLocal() as db:
            service = ScheduleService(db)
            schedule = service._get_schedule(schedule_id)
            if schedule.status in {"completed", "canceled"}:
                return
            if getattr(schedule, reminder_field):
                return
            active_exists = service.active_session_exists_for_schedule(schedule)
            updated = service.mark_reminder_sent(schedule_id, reminder_field)
            lead = self._lead_for_reminder(updated, reminder_field, active_exists)
            self._send_discord_message(updated, lead)
            if reminder_field == "reminder_at_time_sent" and not active_exists:
                service.mark_ready_to_start_notified(schedule_id)

    @staticmethod
    def _lead_for_reminder(schedule: ScheduledCheckout, reminder_field: str, active_exists: bool) -> str:
        if reminder_field == "reminder_30_sent":
            return "Reminder: checkout in 30 minutes."
        if reminder_field == "reminder_10_sent":
            return "Reminder: checkout in 10 minutes."
        if active_exists:
            return "Checkout time is now. Finish the current checkout first, then start this scheduled one."
        return "Checkout time is now. You can start this scheduled checkout below."

    def _send_discord_message(self, schedule: ScheduledCheckout, lead: str) -> None:
        if not settings.discord_bot_token:
            print(f"[Reminder] Missing DISCORD_BOT_TOKEN. Could not send reminder for {schedule.id}.")
            return

        payload = {
            "content": _format_schedule_message(schedule, lead),
            "components": _schedule_components(),
            "allowed_mentions": {"parse": []},
        }
        headers = {
            "Authorization": f"Bot {settings.discord_bot_token}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(timeout=20) as client:
                response = client.post(
                    f"https://discord.com/api/v10/channels/{schedule.discord_channel_id}/messages",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"[Reminder] Failed to send reminder for {schedule.id}: {exc}")
