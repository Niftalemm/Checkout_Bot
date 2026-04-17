import os
import time

from app.reminders.service import ReminderService


def run_loop():
    schedule_path = os.getenv("SCHEDULE_PATH", "data/schedule.json")
    service = ReminderService(schedule_path)
    print("Reminder loop started. Polling every 60 seconds.")
    while True:
        items = service.upcoming(within_minutes=60)
        for reminder in items:
            print(
                "[Reminder] %s, room %s (%s) at %s"
                % (
                    reminder.resident_name,
                    reminder.room_number,
                    reminder.hall,
                    reminder.checkout_at.isoformat(timespec="minutes"),
                )
            )
        time.sleep(60)


if __name__ == "__main__":
    run_loop()
