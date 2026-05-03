from app.core.config import settings
from app.reminders.service import ReminderCoordinator


def run_loop():
    if not settings.standalone_reminders_enabled:
        print("Standalone reminders are disabled. The Discord bot owns scheduled checkout reminders by default.")
        return
    ReminderCoordinator().run_forever()


if __name__ == "__main__":
    run_loop()
