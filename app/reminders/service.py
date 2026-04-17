import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


@dataclass
class Reminder:
    resident_name: str
    room_number: str
    hall: str
    checkout_at: datetime


class ReminderService:
    def __init__(self, schedule_file: str):
        self.schedule_file = Path(schedule_file)

    def upcoming(self, within_minutes: int = 60) -> list[Reminder]:
        if not self.schedule_file.exists():
            return []

        with self.schedule_file.open("r", encoding="utf-8") as f:
            payload = json.load(f)

        now = datetime.now()
        cutoff = now + timedelta(minutes=within_minutes)
        reminders: list[Reminder] = []
        for row in payload.get("checkouts", []):
            checkout_at = datetime.fromisoformat(row["checkout_at"])
            if now <= checkout_at <= cutoff:
                reminders.append(
                    Reminder(
                        resident_name=row["resident_name"],
                        room_number=row["room_number"],
                        hall=row["hall"],
                        checkout_at=checkout_at,
                    )
                )
        return reminders
