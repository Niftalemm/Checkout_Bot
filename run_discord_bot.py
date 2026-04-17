from app.core.config import settings
from app.integrations.discord.bot import run_discord_bot


if __name__ == "__main__":
    if not settings.discord_bot_token:
        raise ValueError("DISCORD_BOT_TOKEN is required.")
    run_discord_bot(settings.discord_bot_token)
