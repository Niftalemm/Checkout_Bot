import os
from dataclasses import dataclass
from datetime import datetime, timezone

import discord
import httpx
from discord import app_commands
from discord.ext import commands


API_BASE = os.getenv("CLAWBOT_API_BASE", "http://127.0.0.1:8000/api")
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()
AUTO_FILL_ON_COMPLETE = os.getenv("AUTO_FILL_ON_COMPLETE", "true").lower() == "true"
VOICE_NOTE_REQUIRE_TEXT = os.getenv("VOICE_NOTE_REQUIRE_TEXT", "true").lower() == "true"


@dataclass
class ActiveCheckout:
    session_id: int
    user_id: int
    channel_id: int
    started_at: datetime


@dataclass
class PendingCheckout:
    user_id: int
    channel_id: int
    started_at: datetime


ACTIVE_BY_CHANNEL: dict[int, ActiveCheckout] = {}
PENDING_BY_CHANNEL: dict[int, PendingCheckout] = {}


def _guild_object() -> discord.Object | None:
    if not DISCORD_GUILD_ID:
        return None
    return discord.Object(id=int(DISCORD_GUILD_ID))


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


async def _create_session(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{API_BASE}/sessions", json=payload)
        response.raise_for_status()
        return response.json()


async def _fetch_summary(session_id: int) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{API_BASE}/sessions/{session_id}/summary")
        response.raise_for_status()
        return response.json()


async def _build_form_draft(session_id: int) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{API_BASE}/sessions/{session_id}/form-draft")
        response.raise_for_status()
        return response.json()


async def _fill_form_draft(session_id: int) -> dict:
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(f"{API_BASE}/sessions/{session_id}/form-draft/fill")
        response.raise_for_status()
        return response.json()


def _parse_header_message(content: str) -> dict | None:
    """
    Expected format:
    resident_name | room_number | tech_id | hall | staff_name | room_side
    """
    parts = [part.strip() for part in content.split("|")]
    if len(parts) != 6 or any(not part for part in parts):
        return None
    return {
        "resident_name": parts[0],
        "room_number": parts[1],
        "tech_id": parts[2],
        "hall": parts[3],
        "staff_name": parts[4],
        "room_side": parts[5],
    }


@bot.event
async def on_ready():
    guild = _guild_object()
    if guild:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()
    print(f"Clawbot logged in as {bot.user} and slash commands synced.")


@bot.tree.command(name="start_checkout", description="Start guided checkout")
async def start_checkout(interaction: discord.Interaction):
    channel_id = interaction.channel_id
    if channel_id is None:
        await interaction.response.send_message(
            "Checkout can only start in a server channel.", ephemeral=True
        )
        return
    if channel_id in ACTIVE_BY_CHANNEL:
        current = ACTIVE_BY_CHANNEL[channel_id]
        await interaction.response.send_message(
            f"A checkout is already active in this channel (session {current.session_id}). "
            "Use `/complete_checkout` first.",
            ephemeral=True,
        )
        return
    if channel_id in PENDING_BY_CHANNEL:
        await interaction.response.send_message(
            "Checkout setup is already pending in this channel. "
            "Send your details message to continue.",
            ephemeral=True,
        )
        return

    PENDING_BY_CHANNEL[channel_id] = PendingCheckout(
        user_id=interaction.user.id,
        channel_id=channel_id,
        started_at=datetime.now(tz=timezone.utc),
    )
    await interaction.response.send_message(
        "Checkout setup started. Send the next message in this format:\n"
        "`resident_name | room_number | tech_id | hall | staff_name | room_side`\n"
        "Example:\n"
        "`Alex Morgan | 104B | T-77 | Maple Hall | Jordan Lee | Left`"
    )


@bot.tree.command(name="summary", description="Show active checkout summary")
async def summary(interaction: discord.Interaction):
    channel_id = interaction.channel_id
    if channel_id is None or channel_id not in ACTIVE_BY_CHANNEL:
        await interaction.response.send_message("No active checkout in this channel.", ephemeral=True)
        return
    current = ACTIVE_BY_CHANNEL[channel_id]
    try:
        data = await _fetch_summary(current.session_id)
    except Exception as exc:  # noqa: BLE001
        await interaction.response.send_message(f"Summary failed: {exc}", ephemeral=True)
        return
    await interaction.response.send_message(
        f"Session {current.session_id}: {data['item_count']} damage item(s), "
        f"total ${data['total_estimated_cost']:.2f}."
    )


@bot.tree.command(name="fill_form_draft", description="Fill Microsoft Form draft now")
@app_commands.describe(session_id="Optional session id; defaults to active session")
async def fill_form_draft(interaction: discord.Interaction, session_id: int | None = None):
    channel_id = interaction.channel_id
    if session_id is None:
        if channel_id is None or channel_id not in ACTIVE_BY_CHANNEL:
            await interaction.response.send_message(
                "No active checkout in this channel and no session id was provided.",
                ephemeral=True,
            )
            return
        session_id = ACTIVE_BY_CHANNEL[channel_id].session_id

    await interaction.response.defer(thinking=True)
    try:
        data = await _fill_form_draft(session_id)
    except Exception as exc:  # noqa: BLE001
        await interaction.followup.send(f"Form fill failed: {exc}")
        return
    await interaction.followup.send(f"Session {session_id}: {data['message']}")


@bot.tree.command(name="prepare_form", description="Prepare form draft and show estimated total")
@app_commands.describe(session_id="Optional session id; defaults to active session")
async def prepare_form(interaction: discord.Interaction, session_id: int | None = None):
    channel_id = interaction.channel_id
    if session_id is None:
        if channel_id is None or channel_id not in ACTIVE_BY_CHANNEL:
            await interaction.response.send_message(
                "No active checkout in this channel and no session id was provided.",
                ephemeral=True,
            )
            return
        session_id = ACTIVE_BY_CHANNEL[channel_id].session_id

    try:
        draft = await _build_form_draft(session_id)
    except Exception as exc:  # noqa: BLE001
        await interaction.response.send_message(f"Prepare form failed: {exc}", ephemeral=True)
        return

    await interaction.response.send_message(
        f"Form draft ready for session {session_id}. "
        f"Total estimated cost: ${draft['total_estimated_cost']:.2f}"
    )


@bot.tree.command(name="complete_checkout", description="Complete active checkout")
async def complete_checkout(interaction: discord.Interaction):
    channel_id = interaction.channel_id
    if channel_id is None or channel_id not in ACTIVE_BY_CHANNEL:
        await interaction.response.send_message("No active checkout in this channel.", ephemeral=True)
        return
    current = ACTIVE_BY_CHANNEL[channel_id]
    if current.user_id != interaction.user.id:
        await interaction.response.send_message(
            "Only the staff member who started this checkout can complete it.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)
    try:
        info = await _fetch_summary(current.session_id)
        form_fill_note = "Skipped auto-fill."
        if AUTO_FILL_ON_COMPLETE:
            fill = await _fill_form_draft(current.session_id)
            form_fill_note = fill.get("message", "Auto-fill executed.")
    except Exception as exc:  # noqa: BLE001
        await interaction.followup.send(f"Complete failed: {exc}")
        return

    del ACTIVE_BY_CHANNEL[channel_id]
    await interaction.followup.send(
        f"Checkout complete for session {current.session_id}.\n"
        f"Items: {info['item_count']} | Total estimated: ${info['total_estimated_cost']:.2f}\n"
        f"Form: {form_fill_note}"
    )


def _is_audio_attachment(attachment: discord.Attachment) -> bool:
    content = (attachment.content_type or "").lower()
    name = attachment.filename.lower()
    return content.startswith("audio/") or name.endswith((".ogg", ".wav", ".mp3", ".m4a", ".webm"))


def _is_image_attachment(attachment: discord.Attachment) -> bool:
    content = (attachment.content_type or "").lower()
    name = attachment.filename.lower()
    return content.startswith("image/") or name.endswith((".jpg", ".jpeg", ".png", ".webp"))


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    channel_id = message.channel.id

    # Step 1: pending checkout setup from next user message.
    if channel_id in PENDING_BY_CHANNEL:
        pending = PENDING_BY_CHANNEL[channel_id]
        if pending.user_id != message.author.id:
            return

        payload = _parse_header_message((message.content or "").strip())
        if payload is None:
            await message.reply(
                "I could not parse that. Use:\n"
                "`resident_name | room_number | tech_id | hall | staff_name | room_side`"
            )
            return

        try:
            session = await _create_session(payload)
        except Exception as exc:  # noqa: BLE001
            await message.reply(f"Failed to create checkout session: {exc}")
            return

        del PENDING_BY_CHANNEL[channel_id]
        ACTIVE_BY_CHANNEL[channel_id] = ActiveCheckout(
            session_id=session["id"],
            user_id=message.author.id,
            channel_id=channel_id,
            started_at=datetime.now(tz=timezone.utc),
        )
        await message.reply(
            f"Checkout started for **{session['resident_name']}** (room {session['room_number']}).\n"
            "Now send damage reports as image + caption or voice note + short text note.\n"
            "Use `/complete_checkout` when done."
        )
        return

    if channel_id not in ACTIVE_BY_CHANNEL:
        return

    current = ACTIVE_BY_CHANNEL[channel_id]
    if current.user_id != message.author.id or not message.attachments:
        return

    image = next((a for a in message.attachments if _is_image_attachment(a)), None)
    audio = next((a for a in message.attachments if _is_audio_attachment(a)), None)
    if not image and not audio:
        return

    note = (message.content or "").strip()
    if audio and VOICE_NOTE_REQUIRE_TEXT and not note:
        await message.reply(
            "Voice note received. Please add a short text note in the same message so I can classify damage."
        )
        return

    raw_note = note
    if not raw_note and image:
        raw_note = "Damage reported from image upload."
    if not raw_note and audio:
        raw_note = "Voice damage note received (no transcript provided)."

    files = None
    if image:
        data = await image.read()
        files = {"image": (image.filename, data, image.content_type or "image/jpeg")}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{API_BASE}/sessions/{current.session_id}/damages",
                data={"raw_note": raw_note},
                files=files,
            )
            response.raise_for_status()
            result = response.json()
    except Exception as exc:  # noqa: BLE001
        await message.reply(f"Could not save damage: {exc}")
        return

    await message.reply(
        f"Damage saved: **{result['cleaned_description']}**\n"
        f"Estimated charge: **${result['estimated_cost']:.2f}** ({result['category']})"
    )


def run_discord_bot(token: str):
    bot.run(token)
