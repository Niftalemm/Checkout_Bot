import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
import httpx
from discord import app_commands
from discord.ext import commands, tasks

from app.services.form_mapping import get_damage_sections


API_BASE = os.getenv("CLAWBOT_API_BASE", "http://127.0.0.1:8000/api")
REVIEW_BASE = os.getenv("CLAWBOT_REVIEW_BASE") or (API_BASE[:-4] if API_BASE.endswith("/api") else API_BASE)
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()
CENTRAL_TZ = ZoneInfo("America/Chicago")
SCHEDULE_ID_PATTERN = re.compile(r"Schedule ID:\s*`?([0-9a-fA-F-]{36})`?")

CATEGORY_LOOKUP = {
    section["key"]: section["key"]
    for section in get_damage_sections()
}
CATEGORY_LOOKUP.update(
    {
        section["name"].lower(): section["key"]
        for section in get_damage_sections()
    }
)
CATEGORY_CHOICES = [
    app_commands.Choice(name=section["name"], value=section["key"])
    for section in get_damage_sections()
]


def _guild_object() -> discord.Object | None:
    if not DISCORD_GUILD_ID:
        return None
    return discord.Object(id=int(DISCORD_GUILD_ID))


def _extract_error_message(exc: Exception, fallback: str) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            payload = exc.response.json()
        except ValueError:
            payload = {}
        return str(payload.get("detail") or fallback)
    if isinstance(exc, httpx.RequestError):
        return "I could not reach the checkout service."
    return fallback


def _normalize_category_choice(text: str) -> str | None:
    normalized = text.strip().lower()
    return CATEGORY_LOOKUP.get(normalized)


async def _category_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    needle = current.strip().lower()
    if not needle:
        return CATEGORY_CHOICES[:25]

    matches = [
        choice
        for choice in CATEGORY_CHOICES
        if needle in choice.name.lower() or needle in choice.value.lower()
    ]
    return matches[:25]


def _format_suggestions(suggestions: list[dict]) -> str:
    lines: list[str] = []
    for index, item in enumerate(suggestions, start=1):
        label = item.get("pricing_name") or item["category_name"]
        quantity = int(item.get("quantity", 1) or 1)
        unit_cost = float(item.get("unit_cost", item.get("estimated_cost", 0.0)) or 0.0)
        total_cost = float(item.get("total_cost", item.get("estimated_cost", 0.0)) or 0.0)
        charged_note = _format_total_cost(total_cost, bool(item.get("chargeable", True)))
        lines.append(f"{index}. {label} (x{quantity}) -> {charged_note}")
        if item.get("pricing_name"):
            lines.append(f"   Category: {item['category_name']}")
        lines.append(
            f"   Confidence: {int(item['confidence'] * 100)}% | "
            f"Unit cost: ${unit_cost:.2f}"
        )
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_total_cost(total_cost: float, chargeable: bool) -> str:
    return "$0.00 (Not charged)" if not chargeable else f"${total_cost:.2f}"


def _extract_schedule_id(content: str) -> str | None:
    match = SCHEDULE_ID_PATTERN.search(content or "")
    if not match:
        return None
    return match.group(1)


def _parse_schedule_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
    return parsed.astimezone(CENTRAL_TZ)


def _format_schedule_card(schedule: dict, lead: str | None = None) -> str:
    scheduled_for = _parse_schedule_time(schedule["checkout_time"]).strftime("%Y-%m-%d %I:%M %p CT")
    lines = []
    if lead:
        lines.append(lead)
    lines.extend(
        [
            f"Resident: **{schedule['resident_name']}**",
            f"Room: **{schedule['room_number']}** | Hall: **{schedule['hall']}** | Side: **{schedule['room_side']}**",
            f"TechID: **{schedule['tech_id']}**",
            f"Checkout time: **{scheduled_for}**",
            f"Status: **{schedule['status']}**",
            f"Schedule ID: `{schedule['id']}`",
        ]
    )
    return "\n".join(lines)


def _format_review_summary(review: dict) -> str:
    lines = [
        f"**Checkout Review**",
        f"Session {review['session_id']}",
        "",
        "**Resident Details**",
        f"Resident: {review['resident_name']}",
        f"Room: {review['room_number']}",
        f"TechID: {review['tech_id']}",
        f"Hall: {review['hall']}",
        f"Staff: {review['staff_name']}",
        f"Side: {review['room_side']}",
        "",
        "**Summary**",
        f"Damages present: {'Yes' if review['has_damages'] else 'No'}",
        f"Confirmed items: {review['item_count']}",
        f"Estimated total: ${review['total_estimated_cost']:.2f}",
        "",
        "**Categories**",
    ]
    for index, section in enumerate(review["sections"], start=1):
        answer = "Yes" if section["has_damage"] else "No"
        description = section["description"] or "None"
        image_note = "Yes" if section["has_image"] else "No"
        lines.extend(
            [
                f"{index}. **{section['category_name']}**",
                f"Answer: {answer}",
                f"Description: {description}",
                f"Estimated cost: ${section['estimated_cost']:.2f}",
                f"Image attached: {image_note}",
                "",
            ]
        )
    lines.extend(
        [
            "**Reply With**",
            "1. Approve and fill the live form",
            "2. Deny and keep editing this checkout",
            "3. Cancel this checkout",
        ]
    )
    return "\n".join(lines)


def _format_damage_prompt(result: dict) -> str:
    suggestion_lines = _format_suggestions(result["suggestions"])
    if result.get("requires_explicit_choice"):
        lead_in = "Pick the best category from the numbered list below."
    else:
        lead_in = "The top suggestion is listed as option 1 below."
    charged_note = _format_total_cost(float(result.get("total_cost", 0.0) or 0.0), bool(result.get("chargeable", True)))
    lines = [
        "**Damage Suggestion**",
        lead_in,
        "",
        f"Description: **{result['cleaned_description']}**",
        f"Quantity: {result.get('quantity', 1)} | Unit cost: ${result.get('unit_cost', 0.0):.2f} | Total: {charged_note}",
        f"Images attached: {result.get('image_count', 0)}",
        "",
        "**Choices**",
        suggestion_lines,
        "0. None of these",
        "X. Cancel Damage",
        "",
        f"Reply with `1`-{len(result['suggestions'])} to choose a category.",
        "Reply with `0` if none are right, then use `change <category>`.",
        "Reply with `cancel damage` to discard it, or send more images to attach them to this pending damage.",
    ]
    return "\n".join(lines)


def _format_awaiting_description_prompt(image_count: int) -> str:
    noun = "image" if image_count == 1 else "images"
    return (
        f"Saved {image_count} {noun} for this damage.\n"
        "Now send the description as text or a voice note.\n"
        "You can also send more images before the description, or reply with `cancel damage`."
    )


def _format_all_categories() -> str:
    lines = ["**All Categories**"]
    for section in get_damage_sections():
        lines.append(f"- {section['name']} (`{section['key']}`)")
    lines.append("")
    lines.append("Reply with `change <category name>` or `change <category key>`.")
    return "\n".join(lines)


def _format_damage_items(items: list[dict]) -> str:
    if not items:
        return "No confirmed damage items yet."

    lines = ["**Damage Items**"]
    for item in items:
        image_count = len(item.get("images") or [])
        image_ids = ", ".join(str(image["id"]) for image in item.get("images") or []) or "None"
        total_cost = float(item.get("total_cost", item.get("estimated_cost", 0.0)) or 0.0)
        total_label = _format_total_cost(total_cost, bool(item.get("chargeable", True)))
        lines.extend(
            [
                f"ID {item['id']}: **{item['category']}**",
                f"Pricing match: {item.get('pricing_name') or 'Manual'}",
                f"Description: {item['cleaned_description']}",
                f"Quantity: {item.get('quantity', 1)} | Unit cost: ${item.get('unit_cost', 0.0):.2f}",
                f"Total cost: {total_label}",
                f"Images: {image_count}",
                f"Image IDs: {image_ids}",
                "",
            ]
        )
    lines.extend(
        [
            "Edit with `/edit_damage_description`, `/edit_damage_category`, `/delete_damage`,",
            "`/add_damage_image`, or `/remove_damage_image`.",
        ]
    )
    return "\n".join(lines).rstrip()


def _parse_pending_capture_choice(content: str, suggestions: list[dict]) -> tuple[str, int | None, str | None]:
    normalized = content.strip().lower()
    if normalized == "confirm":
        return "ok", 0, None
    if normalized in {"x", "cancel", "cancel damage", "cancel damages"}:
        return "cancel", None, None
    direct_category = _normalize_category_choice(normalized)
    if direct_category:
        return "ok", None, direct_category
    if normalized.startswith("change "):
        category_key = _normalize_category_choice(normalized[7:].strip())
        return ("ok", None, category_key) if category_key else ("invalid", None, None)
    if normalized in {"0", "none", "none of these"}:
        return "manual", None, None
    if normalized.isdigit():
        index = int(normalized) - 1
        if 0 <= index < len(suggestions):
            return "ok", index, None
        return "invalid", None, None
    return "ignore", None, None


def _parse_review_action(content: str) -> str | None:
    normalized = content.strip().lower()
    if normalized in {"1", "approve", "yes", "fill_form", "fill form"}:
        return "approve"
    if normalized in {"2", "deny", "no", "cancel_review", "cancel review"}:
        return "deny"
    if normalized in {"3", "cancel_checkout", "cancel checkout"}:
        return "cancel_checkout"
    return None


def _is_cancel_checkout_message(content: str) -> bool:
    return content.strip().lower() in {"cancel_checkout", "cancel checkout"}


def _format_review_actions() -> str:
    return (
        "1. Approve and fill the live form\n"
        "2. Deny and keep editing this checkout\n"
        "3. Cancel this checkout"
    )


async def _send_chunked(channel: discord.abc.Messageable, content: str) -> None:
    chunk_size = 1800
    remaining = content.strip()
    while len(remaining) > chunk_size:
        split_at = remaining.rfind("\n", 0, chunk_size)
        if split_at <= 0:
            split_at = chunk_size
        await channel.send(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        await channel.send(remaining)


async def _start_session(channel_id: int, started_by: int) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{API_BASE}/sessions/discord/start",
            json={"channel_id": channel_id, "started_by": started_by, "source": "discord"},
        )
        response.raise_for_status()
        return response.json()


async def _get_channel_session(channel_id: int) -> dict | None:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{API_BASE}/sessions/active", params={"channel_id": channel_id})
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()


async def _update_session_details(session_id: int, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.put(f"{API_BASE}/sessions/{session_id}/details", json=payload)
        response.raise_for_status()
        return response.json()


async def _capture_damage(
    session_id: int,
    raw_note: str,
    images: list[discord.Attachment] | None = None,
) -> dict:
    files = []
    for image in images or []:
        data = await image.read()
        files.append(("images", (image.filename, data, image.content_type or "image/jpeg")))
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{API_BASE}/sessions/{session_id}/damage-captures",
            data={"raw_note": raw_note},
            files=files,
        )
        response.raise_for_status()
        return response.json()


async def _describe_pending_capture(
    session_id: int,
    capture_id: int,
    raw_note: str | None = None,
    audio: discord.Attachment | None = None,
) -> dict:
    files = None
    if audio is not None:
        data = await audio.read()
        files = {"audio": (audio.filename, data, audio.content_type or "application/octet-stream")}
    form_data = {}
    if raw_note is not None:
        form_data["raw_note"] = raw_note

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{API_BASE}/sessions/{session_id}/damage-captures/{capture_id}/description",
            data=form_data,
            files=files,
        )
        response.raise_for_status()
        return response.json()


async def _add_pending_capture_image(session_id: int, capture_id: int, image: discord.Attachment) -> dict:
    data = await image.read()
    files = {"image": (image.filename, data, image.content_type or "image/jpeg")}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{API_BASE}/sessions/{session_id}/damage-captures/{capture_id}/images",
            files=files,
        )
        response.raise_for_status()
        return response.json()


async def _cancel_pending_capture(session_id: int, capture_id: int) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{API_BASE}/sessions/{session_id}/damage-captures/{capture_id}/cancel"
        )
        response.raise_for_status()
        return response.json()


async def _get_pending_capture(session_id: int) -> dict | None:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{API_BASE}/sessions/{session_id}/pending-capture")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()


async def _confirm_damage(
    session_id: int,
    capture_id: int,
    selection_index: int | None = None,
    category_key: str | None = None,
) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{API_BASE}/sessions/{session_id}/damage-captures/{capture_id}/confirm",
            json={"selection_index": selection_index, "category_key": category_key},
        )
        response.raise_for_status()
        return response.json()


async def _fetch_summary(session_id: int) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{API_BASE}/sessions/{session_id}/summary")
        response.raise_for_status()
        return response.json()


async def _list_damage_items(session_id: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{API_BASE}/sessions/{session_id}/damage-items")
        response.raise_for_status()
        return response.json()


async def _update_damage_description(session_id: int, item_id: int, raw_note: str) -> dict:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.put(
            f"{API_BASE}/sessions/{session_id}/damage-items/{item_id}/description",
            json={"raw_note": raw_note},
        )
        response.raise_for_status()
        return response.json()


async def _update_damage_category(session_id: int, item_id: int, category_key: str) -> dict:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.put(
            f"{API_BASE}/sessions/{session_id}/damage-items/{item_id}/category",
            json={"category_key": category_key},
        )
        response.raise_for_status()
        return response.json()


async def _delete_damage_item(session_id: int, item_id: int) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.delete(f"{API_BASE}/sessions/{session_id}/damage-items/{item_id}")
        response.raise_for_status()
        return response.json()


async def _add_damage_image(session_id: int, item_id: int, image: discord.Attachment) -> dict:
    data = await image.read()
    files = {"image": (image.filename, data, image.content_type or "image/jpeg")}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{API_BASE}/sessions/{session_id}/damage-items/{item_id}/images",
            files=files,
        )
        response.raise_for_status()
        return response.json()


async def _remove_damage_image(session_id: int, item_id: int, image_id: int) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.delete(
            f"{API_BASE}/sessions/{session_id}/damage-items/{item_id}/images/{image_id}"
        )
        response.raise_for_status()
        return response.json()


async def _request_review(session_id: int) -> dict:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(f"{API_BASE}/sessions/{session_id}/review")
        response.raise_for_status()
        return response.json()


async def _cancel_review(session_id: int) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{API_BASE}/sessions/{session_id}/review/cancel")
        response.raise_for_status()
        return response.json()


async def _cancel_session(session_id: int) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{API_BASE}/sessions/{session_id}/cancel")
        response.raise_for_status()
        return response.json()


async def _create_scheduled_checkout(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{API_BASE}/scheduled-checkouts", json=payload)
        response.raise_for_status()
        return response.json()


async def _list_scheduled_checkouts(user_id: int, include_terminal: bool = False) -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{API_BASE}/scheduled-checkouts",
            params={"creator_discord_user_id": str(user_id), "include_terminal": include_terminal},
        )
        response.raise_for_status()
        return response.json()


async def _get_scheduled_checkout(schedule_id: str, user_id: int) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{API_BASE}/scheduled-checkouts/{schedule_id}",
            params={"creator_discord_user_id": str(user_id)},
        )
        response.raise_for_status()
        return response.json()


async def _update_scheduled_checkout(schedule_id: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.put(f"{API_BASE}/scheduled-checkouts/{schedule_id}", json=payload)
        response.raise_for_status()
        return response.json()


async def _cancel_scheduled_checkout(schedule_id: str, user_id: int) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{API_BASE}/scheduled-checkouts/{schedule_id}/cancel",
            json={"creator_discord_user_id": str(user_id)},
        )
        response.raise_for_status()
        return response.json()


async def _start_scheduled_checkout(schedule_id: str, user_id: int, display_name: str, channel_id: int) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{API_BASE}/scheduled-checkouts/{schedule_id}/start",
            json={
                "creator_discord_user_id": str(user_id),
                "creator_display_name": display_name,
                "discord_channel_id": str(channel_id),
            },
        )
        response.raise_for_status()
        return response.json()


async def _next_ready_scheduled_checkout(user_id: int, channel_id: int, only_unnotified: bool = False) -> dict | None:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{API_BASE}/scheduled-checkouts-ready-next",
            params={
                "creator_discord_user_id": str(user_id),
                "discord_channel_id": str(channel_id),
                "only_unnotified": only_unnotified,
            },
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()


async def _mark_ready_scheduled_checkout_notified(schedule_id: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{API_BASE}/scheduled-checkouts/{schedule_id}/mark-ready-notified")
        response.raise_for_status()
        return response.json()


async def _list_due_scheduled_checkout_reminders() -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{API_BASE}/scheduled-checkouts-due-reminders")
        response.raise_for_status()
        return response.json()


async def _mark_scheduled_checkout_reminder_sent(schedule_id: str, reminder_field: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{API_BASE}/scheduled-checkouts/{schedule_id}/mark-reminder-sent",
            params={"reminder_field": reminder_field},
        )
        response.raise_for_status()
        return response.json()


async def _build_form_draft(session_id: int) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{API_BASE}/sessions/{session_id}/form-draft")
        response.raise_for_status()
        return response.json()


async def _fill_form_draft(session_id: int) -> dict:
    async with httpx.AsyncClient(timeout=1800) as client:
        response = await client.post(f"{API_BASE}/sessions/{session_id}/form-draft/fill")
        response.raise_for_status()
        return response.json()


async def _complete_session(session_id: int) -> dict:
    async with httpx.AsyncClient(timeout=1800) as client:
        response = await client.post(f"{API_BASE}/sessions/{session_id}/complete")
        response.raise_for_status()
        return response.json()


def _parse_header_message(content: str) -> dict | None:
    normalized = content.strip()
    if normalized.startswith("```") and normalized.endswith("```"):
        normalized = normalized[3:-3].strip()

    parts = [line.strip() for line in normalized.splitlines() if line.strip()]
    if len(parts) == 6:
        parts = [parts[0], parts[1], parts[2], parts[3], parts[5]]
    if len(parts) != 5 or any(not part for part in parts):
        return None
    return {
        "resident_name": parts[0],
        "room_number": parts[1],
        "tech_id": parts[2],
        "hall": parts[3],
        "room_side": parts[4],
        "staff_name": "Nift",
    }


class ScheduledCheckoutEditModal(discord.ui.Modal, title="Edit Scheduled Checkout"):
    def __init__(self, schedule: dict, user_id: int):
        super().__init__()
        self.schedule_id = schedule["id"]
        self.user_id = user_id
        central_time = _parse_schedule_time(schedule["checkout_time"])
        self.resident_name = discord.ui.TextInput(
            label="Resident Name",
            default=schedule["resident_name"],
            max_length=120,
        )
        self.room_number = discord.ui.TextInput(
            label="Room Number",
            default=schedule["room_number"],
            max_length=20,
        )
        self.tech_id = discord.ui.TextInput(
            label="TechID",
            default=schedule["tech_id"],
            max_length=40,
        )
        self.hall_and_side = discord.ui.TextInput(
            label="Hall|Side",
            default=f"{schedule['hall']}|{schedule['room_side']}",
            max_length=20,
        )
        self.date_and_time = discord.ui.TextInput(
            label="YYYY-MM-DD HH:MM (Central)",
            default=central_time.strftime("%Y-%m-%d %H:%M"),
            max_length=16,
        )
        self.add_item(self.resident_name)
        self.add_item(self.room_number)
        self.add_item(self.tech_id)
        self.add_item(self.hall_and_side)
        self.add_item(self.date_and_time)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            hall_value, room_side_value = [part.strip() for part in self.hall_and_side.value.split("|", 1)]
            date_value, time_value = self.date_and_time.value.split(None, 1)
        except ValueError:
            await interaction.response.send_message(
                "Use `Hall|Side` like `A|left` and date/time like `2026-05-01 14:30`.",
                ephemeral=True,
            )
            return
        try:
            updated = await _update_scheduled_checkout(
                self.schedule_id,
                {
                    "creator_discord_user_id": str(self.user_id),
                    "resident_name": self.resident_name.value,
                    "room_number": self.room_number.value,
                    "tech_id": self.tech_id.value,
                    "hall": hall_value,
                    "room_side": room_side_value,
                    "checkout_date": date_value.strip(),
                    "checkout_time": time_value.strip(),
                },
            )
        except Exception as exc:  # noqa: BLE001
            await interaction.response.send_message(
                _extract_error_message(exc, "I could not update that scheduled checkout."),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Updated scheduled checkout.\n{_format_schedule_card(updated)}",
            ephemeral=True,
        )


class ScheduledCheckoutCreateModal(discord.ui.Modal, title="Schedule Checkout"):
    def __init__(
        self,
        user_id: int,
        display_name: str,
        channel_id: int,
        defaults: dict[str, str | None] | None = None,
    ):
        super().__init__()
        defaults = defaults or {}
        self.user_id = user_id
        self.display_name = display_name
        self.channel_id = channel_id
        hall = defaults.get("hall") or ""
        room_side = defaults.get("room_side") or ""
        date_value = defaults.get("checkout_date") or ""
        time_value = defaults.get("checkout_time") or ""
        self.resident_name = discord.ui.TextInput(
            label="Resident Name",
            default=defaults.get("resident_name"),
            max_length=120,
        )
        self.room_number = discord.ui.TextInput(
            label="Room Number",
            default=defaults.get("room_number"),
            max_length=20,
        )
        self.tech_id = discord.ui.TextInput(
            label="TechID",
            default=defaults.get("tech_id"),
            max_length=40,
        )
        self.hall_and_side = discord.ui.TextInput(
            label="Hall|Side",
            default=f"{hall}|{room_side}" if hall or room_side else None,
            placeholder="A|left",
            max_length=20,
        )
        self.date_and_time = discord.ui.TextInput(
            label="YYYY-MM-DD HH:MM (Central)",
            default=f"{date_value} {time_value}".strip() or None,
            placeholder="2026-05-01 14:30",
            max_length=16,
        )
        self.add_item(self.resident_name)
        self.add_item(self.room_number)
        self.add_item(self.tech_id)
        self.add_item(self.hall_and_side)
        self.add_item(self.date_and_time)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            hall_value, room_side_value = [part.strip() for part in self.hall_and_side.value.split("|", 1)]
            date_value, time_value = self.date_and_time.value.split(None, 1)
        except ValueError:
            await interaction.response.send_message(
                "Use `Hall|Side` like `A|left` and date/time like `2026-05-01 14:30`.",
                ephemeral=True,
            )
            return
        try:
            schedule = await _create_scheduled_checkout(
                {
                    "resident_name": self.resident_name.value,
                    "room_number": self.room_number.value,
                    "tech_id": self.tech_id.value,
                    "hall": hall_value,
                    "room_side": room_side_value,
                    "checkout_date": date_value.strip(),
                    "checkout_time": time_value.strip(),
                    "creator_discord_user_id": str(self.user_id),
                    "creator_display_name": self.display_name,
                    "discord_channel_id": str(self.channel_id),
                }
            )
        except Exception as exc:  # noqa: BLE001
            await interaction.response.send_message(
                _extract_error_message(exc, "I could not schedule that checkout."),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            _format_schedule_card(schedule, "Scheduled checkout created."),
            view=ScheduledCheckoutActionsView(),
            ephemeral=True,
        )


class ScheduledCheckoutActionsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _schedule_id_from_interaction(self, interaction: discord.Interaction) -> str | None:
        content = interaction.message.content if interaction.message else ""
        return _extract_schedule_id(content)

    @discord.ui.button(label="Start", style=discord.ButtonStyle.success, custom_id="schedule_start")
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        schedule_id = await self._schedule_id_from_interaction(interaction)
        if not schedule_id or interaction.channel_id is None:
            await interaction.response.send_message("I could not find that scheduled checkout.", ephemeral=True)
            return
        try:
            result = await _start_scheduled_checkout(
                schedule_id,
                interaction.user.id,
                interaction.user.display_name,
                interaction.channel_id,
            )
        except Exception as exc:  # noqa: BLE001
            await interaction.response.send_message(
                _extract_error_message(exc, "I could not start that scheduled checkout."),
                ephemeral=True,
            )
            return

        if result["status"] == "blocked":
            await interaction.response.send_message(result["message"], ephemeral=True)
            return

        await interaction.response.send_message(
            f"Scheduled checkout started for **{result['schedule']['resident_name']}** in room **{result['schedule']['room_number']}**.\n"
            "You can start sending damage descriptions now.",
            ephemeral=True,
        )
        if interaction.channel:
            await interaction.channel.send(
                f"Scheduled checkout started for **{result['schedule']['resident_name']}** "
                f"(room {result['schedule']['room_number']})."
            )

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.secondary, custom_id="schedule_edit")
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        schedule_id = await self._schedule_id_from_interaction(interaction)
        if not schedule_id:
            await interaction.response.send_message("I could not find that scheduled checkout.", ephemeral=True)
            return
        try:
            schedule = await _get_scheduled_checkout(schedule_id, interaction.user.id)
        except Exception as exc:  # noqa: BLE001
            await interaction.response.send_message(
                _extract_error_message(exc, "I could not load that scheduled checkout."),
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(ScheduledCheckoutEditModal(schedule, interaction.user.id))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, custom_id="schedule_cancel")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        schedule_id = await self._schedule_id_from_interaction(interaction)
        if not schedule_id:
            await interaction.response.send_message("I could not find that scheduled checkout.", ephemeral=True)
            return
        try:
            canceled = await _cancel_scheduled_checkout(schedule_id, interaction.user.id)
        except Exception as exc:  # noqa: BLE001
            await interaction.response.send_message(
                _extract_error_message(exc, "I could not cancel that scheduled checkout."),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Canceled scheduled checkout for **{canceled['resident_name']}**.",
            ephemeral=True,
        )


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
bot.add_view(ScheduledCheckoutActionsView())


@bot.event
async def on_ready():
    guild = _guild_object()
    if guild:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()
    if not scheduled_checkout_reminder_loop.is_running():
        scheduled_checkout_reminder_loop.start()
    print(f"Clawbot logged in as {bot.user} and slash commands synced.")


@tasks.loop(seconds=45)
async def scheduled_checkout_reminder_loop() -> None:
    try:
        reminders = await _list_due_scheduled_checkout_reminders()
    except Exception as exc:  # noqa: BLE001
        print(f"[Scheduled reminders] Could not load due reminders: {exc}")
        return

    for reminder in reminders:
        schedule = reminder["schedule"]
        try:
            channel_id = int(schedule["discord_channel_id"])
        except (TypeError, ValueError):
            continue
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except Exception as exc:  # noqa: BLE001
                print(f"[Scheduled reminders] Could not fetch channel {schedule['discord_channel_id']}: {exc}")
                continue
        try:
            await channel.send(
                content=_format_schedule_card(schedule, reminder["lead"]),
                view=ScheduledCheckoutActionsView(),
            )
            await _mark_scheduled_checkout_reminder_sent(schedule["id"], reminder["reminder_field"])
        except Exception as exc:  # noqa: BLE001
            print(f"[Scheduled reminders] Could not send reminder for {schedule['id']}: {exc}")


@scheduled_checkout_reminder_loop.before_loop
async def before_scheduled_checkout_reminder_loop() -> None:
    await bot.wait_until_ready()


async def _resolve_session_for_command(
    interaction: discord.Interaction, session_id: int | None = None
) -> dict | None:
    if session_id is not None:
        return {"id": session_id}
    if interaction.channel_id is None:
        return None
    return await _get_channel_session(interaction.channel_id)


async def _send_schedule_entries(target, schedules: list[dict], lead: str, ephemeral: bool = True) -> None:
    is_followup = hasattr(target, "response")
    if hasattr(target, "response"):
        await target.response.defer(ephemeral=ephemeral)
        sender = target.followup.send
    else:
        sender = target.send

    first = True
    for schedule in schedules:
        kwargs = {
            "content": _format_schedule_card(schedule, lead if first else None),
            "view": ScheduledCheckoutActionsView(),
        }
        if is_followup:
            kwargs["ephemeral"] = ephemeral
        await sender(**kwargs)
        first = False


async def _send_next_ready_schedule_prompt(channel: discord.abc.Messageable | None, user_id: int, channel_id: int) -> None:
    if channel is None:
        return
    try:
        schedule = await _next_ready_scheduled_checkout(user_id, channel_id, only_unnotified=True)
    except Exception:
        return
    if not schedule:
        return
    await channel.send(
        content=_format_schedule_card(
            schedule,
            "You finished the current checkout. This scheduled checkout is ready to start next:",
        ),
        view=ScheduledCheckoutActionsView(),
    )
    try:
        await _mark_ready_scheduled_checkout_notified(schedule["id"])
    except Exception:
        return


@bot.tree.command(name="start_checkout", description="Start guided checkout")
async def start_checkout(interaction: discord.Interaction):
    channel_id = interaction.channel_id
    if channel_id is None:
        await interaction.response.send_message(
            "Checkout can only start in a server channel.", ephemeral=True
        )
        return

    try:
        await _start_session(channel_id=channel_id, started_by=interaction.user.id)
    except Exception as exc:  # noqa: BLE001
        await interaction.response.send_message(
            _extract_error_message(exc, "I could not start a new checkout right now."),
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        "Checkout setup started. Send the next message with one field per line in this order:\n"
        "`resident_name`\n"
        "`room_number`\n"
        "`tech_id`\n"
        "`hall` (`A`, `B`, `C`, or `D`)\n"
        "`room_side` (`left`, `right`, or `single`)\n"
        "Example:\n"
        "```text\n"
        "Alex Morgan\n"
        "104B\n"
        "T-77\n"
        "A\n"
        "left\n"
        "```"
    )


@bot.tree.command(name="schedule_checkout", description="Schedule a future resident checkout in Central time")
@app_commands.describe(
    resident_name="Resident name",
    room_number="Room number",
    tech_id="TechID",
    hall="Hall letter",
    room_side="Room side",
    checkout_date="YYYY-MM-DD",
    checkout_time="HH:MM in Central time, 24-hour",
)
async def schedule_checkout(
    interaction: discord.Interaction,
    resident_name: str | None = None,
    room_number: str | None = None,
    tech_id: str | None = None,
    hall: str | None = None,
    room_side: str | None = None,
    checkout_date: str | None = None,
    checkout_time: str | None = None,
):
    if interaction.channel_id is None:
        await interaction.response.send_message("Scheduling only works in a server channel.", ephemeral=True)
        return
    schedule_fields = {
        "resident_name": resident_name,
        "room_number": room_number,
        "tech_id": tech_id,
        "hall": hall,
        "room_side": room_side,
        "checkout_date": checkout_date,
        "checkout_time": checkout_time,
    }
    if not all(schedule_fields.values()):
        await interaction.response.send_modal(
            ScheduledCheckoutCreateModal(
                interaction.user.id,
                interaction.user.display_name,
                interaction.channel_id,
                schedule_fields,
            )
        )
        return
    try:
        schedule = await _create_scheduled_checkout(
            {
                "resident_name": resident_name,
                "room_number": room_number,
                "tech_id": tech_id,
                "hall": hall,
                "room_side": room_side,
                "checkout_date": checkout_date,
                "checkout_time": checkout_time,
                "creator_discord_user_id": str(interaction.user.id),
                "creator_display_name": interaction.user.display_name,
                "discord_channel_id": str(interaction.channel_id),
            }
        )
    except Exception as exc:  # noqa: BLE001
        await interaction.response.send_message(
            _extract_error_message(exc, "I could not schedule that checkout."),
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        _format_schedule_card(schedule, "Scheduled checkout created."),
        view=ScheduledCheckoutActionsView(),
        ephemeral=True,
    )


@bot.tree.command(name="my_schedule", description="Show your scheduled checkouts")
async def my_schedule(interaction: discord.Interaction):
    try:
        schedules = await _list_scheduled_checkouts(interaction.user.id)
    except Exception as exc:  # noqa: BLE001
        await interaction.response.send_message(
            _extract_error_message(exc, "I could not load your schedule."),
            ephemeral=True,
        )
        return
    if not schedules:
        await interaction.response.send_message("You have no upcoming scheduled checkouts.", ephemeral=True)
        return
    await _send_schedule_entries(interaction, schedules[:10], "Your scheduled checkouts:", ephemeral=True)


@bot.tree.command(name="edit_scheduled_checkout", description="Edit one of your scheduled checkouts")
@app_commands.describe(
    schedule_id="Scheduled checkout ID",
    resident_name="Optional new resident name",
    room_number="Optional new room number",
    tech_id="Optional new TechID",
    hall="Optional new hall",
    room_side="Optional new room side",
    checkout_date="Optional new date YYYY-MM-DD",
    checkout_time="Optional new time HH:MM in Central time",
)
async def edit_scheduled_checkout(
    interaction: discord.Interaction,
    schedule_id: str,
    resident_name: str | None = None,
    room_number: str | None = None,
    tech_id: str | None = None,
    hall: str | None = None,
    room_side: str | None = None,
    checkout_date: str | None = None,
    checkout_time: str | None = None,
):
    if not any([resident_name, room_number, tech_id, hall, room_side, checkout_date, checkout_time]):
        await interaction.response.send_message("Provide at least one field to update.", ephemeral=True)
        return
    try:
        schedule = await _update_scheduled_checkout(
            schedule_id,
            {
                "creator_discord_user_id": str(interaction.user.id),
                "resident_name": resident_name,
                "room_number": room_number,
                "tech_id": tech_id,
                "hall": hall,
                "room_side": room_side,
                "checkout_date": checkout_date,
                "checkout_time": checkout_time,
            },
        )
    except Exception as exc:  # noqa: BLE001
        await interaction.response.send_message(
            _extract_error_message(exc, "I could not update that scheduled checkout."),
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        _format_schedule_card(schedule, "Scheduled checkout updated."),
        view=ScheduledCheckoutActionsView(),
        ephemeral=True,
    )


@bot.tree.command(name="cancel_scheduled_checkout", description="Cancel one of your scheduled checkouts")
@app_commands.describe(schedule_id="Scheduled checkout ID")
async def cancel_scheduled_checkout(interaction: discord.Interaction, schedule_id: str):
    try:
        schedule = await _cancel_scheduled_checkout(schedule_id, interaction.user.id)
    except Exception as exc:  # noqa: BLE001
        await interaction.response.send_message(
            _extract_error_message(exc, "I could not cancel that scheduled checkout."),
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"Canceled scheduled checkout for **{schedule['resident_name']}**.",
        ephemeral=True,
    )


@bot.tree.command(name="summary", description="Show active checkout summary")
async def summary(interaction: discord.Interaction):
    if interaction.channel_id is None:
        await interaction.response.send_message("No active checkout in this channel.", ephemeral=True)
        return

    try:
        current = await _get_channel_session(interaction.channel_id)
        if not current or current.get("status") != "active":
            await interaction.response.send_message(
                "No active checkout in this channel.", ephemeral=True
            )
            return
        data = await _fetch_summary(current["id"])
    except Exception as exc:  # noqa: BLE001
        await interaction.response.send_message(
            _extract_error_message(exc, "I could not load the checkout summary."),
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"Session {current['id']}: {data['item_count']} confirmed damage item(s), "
        f"total ${data['total_estimated_cost']:.2f}. Use `/list_damages` to edit them."
    )


@bot.tree.command(name="list_damages", description="List confirmed damage items for the active checkout")
async def list_damages(interaction: discord.Interaction):
    if interaction.channel_id is None:
        await interaction.response.send_message("No active checkout in this channel.", ephemeral=True)
        return

    try:
        current = await _get_channel_session(interaction.channel_id)
        if not current or current.get("status") != "active":
            await interaction.response.send_message(
                "No active checkout in this channel.", ephemeral=True
            )
            return
        items = await _list_damage_items(current["id"])
    except Exception as exc:  # noqa: BLE001
        await interaction.response.send_message(
            _extract_error_message(exc, "I could not list the saved damage items."),
            ephemeral=True,
        )
        return

    await interaction.response.send_message(_format_damage_items(items), ephemeral=True)


@bot.tree.command(name="edit_damage_description", description="Rewrite and recalculate one saved damage item")
@app_commands.describe(item_id="Damage item ID", new_text="New short damage note")
async def edit_damage_description(
    interaction: discord.Interaction,
    item_id: int,
    new_text: str,
):
    if interaction.channel_id is None:
        await interaction.response.send_message("No active checkout in this channel.", ephemeral=True)
        return

    try:
        current = await _get_channel_session(interaction.channel_id)
        if not current or current.get("status") != "active":
            await interaction.response.send_message(
                "No active checkout in this channel.", ephemeral=True
            )
            return
        item = await _update_damage_description(current["id"], item_id, new_text)
    except Exception as exc:  # noqa: BLE001
        await interaction.response.send_message(
            _extract_error_message(exc, "I could not update that damage description."),
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"Updated item {item['id']}.\n"
        f"{item['cleaned_description']} | x{item.get('quantity', 1)} | "
        f"${item.get('unit_cost', 0.0):.2f} each | "
        f"{_format_total_cost(float(item.get('total_cost', item['estimated_cost']) or 0.0), bool(item.get('chargeable', True)))}",
        ephemeral=True,
    )


@bot.tree.command(name="edit_damage_category", description="Change the category for one saved damage item")
@app_commands.describe(item_id="Damage item ID", category="Category key or category name")
@app_commands.autocomplete(category=_category_autocomplete)
async def edit_damage_category(
    interaction: discord.Interaction,
    item_id: int,
    category: str,
):
    if interaction.channel_id is None:
        await interaction.response.send_message("No active checkout in this channel.", ephemeral=True)
        return

    category_key = _normalize_category_choice(category)
    if not category_key:
        await interaction.response.send_message(
            "That category was not recognized.", ephemeral=True
        )
        return

    try:
        current = await _get_channel_session(interaction.channel_id)
        if not current or current.get("status") != "active":
            await interaction.response.send_message(
                "No active checkout in this channel.", ephemeral=True
            )
            return
        item = await _update_damage_category(current["id"], item_id, category_key)
    except Exception as exc:  # noqa: BLE001
        await interaction.response.send_message(
            _extract_error_message(exc, "I could not change that damage category."),
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"Updated item {item['id']} to **{item['category']}**.\n"
        f"{item['cleaned_description']} | x{item.get('quantity', 1)} | "
        f"${item.get('unit_cost', 0.0):.2f} each | "
        f"{_format_total_cost(float(item.get('total_cost', item['estimated_cost']) or 0.0), bool(item.get('chargeable', True)))}",
        ephemeral=True,
    )


@bot.tree.command(name="delete_damage", description="Delete one saved damage item")
@app_commands.describe(item_id="Damage item ID")
async def delete_damage(interaction: discord.Interaction, item_id: int):
    if interaction.channel_id is None:
        await interaction.response.send_message("No active checkout in this channel.", ephemeral=True)
        return

    try:
        current = await _get_channel_session(interaction.channel_id)
        if not current or current.get("status") != "active":
            await interaction.response.send_message(
                "No active checkout in this channel.", ephemeral=True
            )
            return
        await _delete_damage_item(current["id"], item_id)
    except Exception as exc:  # noqa: BLE001
        await interaction.response.send_message(
            _extract_error_message(exc, "I could not delete that damage item."),
            ephemeral=True,
        )
        return

    await interaction.response.send_message(f"Deleted item {item_id}.", ephemeral=True)


@bot.tree.command(name="add_damage_image", description="Attach another image to a saved damage item")
@app_commands.describe(item_id="Damage item ID", image="Image attachment")
async def add_damage_image(
    interaction: discord.Interaction,
    item_id: int,
    image: discord.Attachment,
):
    if interaction.channel_id is None:
        await interaction.response.send_message("No active checkout in this channel.", ephemeral=True)
        return
    if not _is_image_attachment(image):
        await interaction.response.send_message("Please attach an image file.", ephemeral=True)
        return

    try:
        current = await _get_channel_session(interaction.channel_id)
        if not current or current.get("status") != "active":
            await interaction.response.send_message(
                "No active checkout in this channel.", ephemeral=True
            )
            return
        item = await _add_damage_image(current["id"], item_id, image)
    except Exception as exc:  # noqa: BLE001
        await interaction.response.send_message(
            _extract_error_message(exc, "I could not add that image."),
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"Added image to item {item['id']}. Total images: {len(item.get('images') or [])}.",
        ephemeral=True,
    )


@bot.tree.command(name="remove_damage_image", description="Remove one image from a saved damage item")
@app_commands.describe(item_id="Damage item ID", image_id="Damage image ID")
async def remove_damage_image(interaction: discord.Interaction, item_id: int, image_id: int):
    if interaction.channel_id is None:
        await interaction.response.send_message("No active checkout in this channel.", ephemeral=True)
        return

    try:
        current = await _get_channel_session(interaction.channel_id)
        if not current or current.get("status") != "active":
            await interaction.response.send_message(
                "No active checkout in this channel.", ephemeral=True
            )
            return
        item = await _remove_damage_image(current["id"], item_id, image_id)
    except Exception as exc:  # noqa: BLE001
        await interaction.response.send_message(
            _extract_error_message(exc, "I could not remove that image."),
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"Removed image {image_id} from item {item['id']}. Remaining images: {len(item.get('images') or [])}.",
        ephemeral=True,
    )


@bot.tree.command(name="prepare_form", description="Build the form draft without filling the live form")
@app_commands.describe(session_id="Optional session id; defaults to active session")
async def prepare_form(interaction: discord.Interaction, session_id: int | None = None):
    try:
        session = await _resolve_session_for_command(interaction, session_id)
        if not session:
            await interaction.response.send_message(
                "No active checkout in this channel and no session id was provided.",
                ephemeral=True,
            )
            return
        draft = await _build_form_draft(session["id"])
    except Exception as exc:  # noqa: BLE001
        await interaction.response.send_message(
            _extract_error_message(exc, "I could not prepare the form draft."),
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"Draft ready for session {session['id']}. "
        f"Categories prepared: {sum(1 for section in draft['sections'] if section['answer_yes_no'] == 'Yes')}."
    )


@bot.tree.command(name="review_page", description="Show the web review page link for this checkout")
@app_commands.describe(session_id="Optional session id; defaults to active session")
async def review_page(interaction: discord.Interaction, session_id: int | None = None):
    try:
        session = await _resolve_session_for_command(interaction, session_id)
        if not session:
            await interaction.response.send_message(
                "No active checkout in this channel and no session id was provided.",
                ephemeral=True,
            )
            return
    except Exception as exc:  # noqa: BLE001
        await interaction.response.send_message(
            _extract_error_message(exc, "I could not find that checkout."),
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"Review page: {REVIEW_BASE}/api/review/{session['id']}",
        ephemeral=True,
    )


@bot.tree.command(name="fill_form_draft", description="Retry the live Microsoft Form fill for a session")
@app_commands.describe(session_id="Optional session id; defaults to active session")
async def fill_form_draft(interaction: discord.Interaction, session_id: int | None = None):
    try:
        session = await _resolve_session_for_command(interaction, session_id)
        if not session:
            await interaction.response.send_message(
                "No active checkout in this channel and no session id was provided.",
                ephemeral=True,
            )
            return
    except Exception as exc:  # noqa: BLE001
        await interaction.response.send_message(
            _extract_error_message(exc, "I could not find that checkout."),
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)
    await interaction.followup.send(
        f"Starting Microsoft Form fill for session {session['id']}.\n"
        f"Review page: {REVIEW_BASE}/api/review/{session['id']}"
    )
    try:
        data = await _fill_form_draft(session["id"])
    except Exception as exc:  # noqa: BLE001
        await interaction.followup.send(
            f"{_extract_error_message(exc, 'Automatic form fill failed. Progress was saved for retry.')}\n"
            f"Review page: {REVIEW_BASE}/api/review/{session['id']}"
        )
        return
    await interaction.followup.send(
        f"Session {session['id']}: {data['message']}\n"
        f"Review page: {REVIEW_BASE}/api/review/{session['id']}"
    )


@bot.tree.command(name="complete_checkout", description="Review the checkout and wait for approval")
async def complete_checkout(interaction: discord.Interaction):
    if interaction.channel_id is None:
        await interaction.response.send_message("No active checkout in this channel.", ephemeral=True)
        return

    try:
        current = await _get_channel_session(interaction.channel_id)
        if not current or current.get("status") != "active":
            await interaction.response.send_message(
                "No active checkout in this channel.", ephemeral=True
            )
            return
        if str(current.get("started_by") or "") != str(interaction.user.id):
            await interaction.response.send_message(
                "Only the staff member who started this checkout can review or complete it.",
                ephemeral=True,
            )
            return
        review = await _request_review(current["id"])
    except Exception as exc:  # noqa: BLE001
        await interaction.response.send_message(
            _extract_error_message(exc, "I could not prepare the checkout review."),
            ephemeral=True,
        )
        return

    await interaction.response.send_message("Checkout review is ready below.")
    await _send_chunked(interaction.channel, _format_review_summary(review))


@bot.tree.command(name="cancel_checkout", description="Cancel the active checkout in this channel")
async def cancel_checkout(interaction: discord.Interaction):
    if interaction.channel_id is None:
        await interaction.response.send_message("No active checkout in this channel.", ephemeral=True)
        return

    try:
        current = await _get_channel_session(interaction.channel_id)
        if not current:
            await interaction.response.send_message(
                "No active checkout in this channel.", ephemeral=True
            )
            return
        if str(current.get("started_by") or "") != str(interaction.user.id):
            await interaction.response.send_message(
                "Only the staff member who started this checkout can cancel it.",
                ephemeral=True,
            )
            return
        await _cancel_session(current["id"])
    except Exception as exc:  # noqa: BLE001
        await interaction.response.send_message(
            _extract_error_message(exc, "I could not cancel this checkout."),
            ephemeral=True,
        )
        return

    await interaction.response.send_message("Checkout canceled. You can start a new one with `/start_checkout`.")
    await _send_next_ready_schedule_prompt(interaction.channel, interaction.user.id, interaction.channel_id)


def _is_audio_attachment(attachment: discord.Attachment) -> bool:
    content = (attachment.content_type or "").lower()
    name = attachment.filename.lower()
    return content.startswith("audio/") or name.endswith(
        (".ogg", ".wav", ".mp3", ".m4a", ".webm", ".mp4", ".mpeg", ".mpga", ".flac")
    )


def _is_image_attachment(attachment: discord.Attachment) -> bool:
    content = (attachment.content_type or "").lower()
    name = attachment.filename.lower()
    return content.startswith("image/") or name.endswith((".jpg", ".jpeg", ".png", ".webp"))


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    try:
        current = await _get_channel_session(message.channel.id)
    except Exception:
        return

    if not current:
        return
    if str(current.get("started_by") or "") != str(message.author.id):
        return

    content = (message.content or "").strip()
    image_attachments = [attachment for attachment in message.attachments if _is_image_attachment(attachment)]
    audio_attachment = next((attachment for attachment in message.attachments if _is_audio_attachment(attachment)), None)
    unsupported_attachments = [
        attachment
        for attachment in message.attachments
        if not _is_image_attachment(attachment) and not _is_audio_attachment(attachment)
    ]

    if _is_cancel_checkout_message(content):
        try:
            await _cancel_session(current["id"])
        except Exception as exc:  # noqa: BLE001
            await message.reply(_extract_error_message(exc, "I could not cancel this checkout."))
            return
        await message.reply("Checkout canceled. You can start a new one with `/start_checkout`.")
        await _send_next_ready_schedule_prompt(message.channel, message.author.id, message.channel.id)
        return

    if current.get("status") == "pending_details":
        payload = _parse_header_message(content)
        if payload is None:
            await message.reply(
                "I could not parse that. Send one field per line in this order:\n"
                "```text\n"
                "resident_name\n"
                "room_number\n"
                "tech_id\n"
                "hall\n"
                "room_side\n"
                "```"
            )
            return

        try:
            session = await _update_session_details(current["id"], payload)
        except Exception as exc:  # noqa: BLE001
            await message.reply(_extract_error_message(exc, "I could not save those checkout details."))
            return

        await message.reply(
            f"Checkout started for **{session['resident_name']}** (room {session['room_number']}).\n"
            "Send a short damage description to start a pending damage.\n"
            "You can attach images in the same message, or send images first and the description right after as text or a voice note.\n"
            "I will suggest matches, then you can pick one with a number."
        )
        return

    pending_capture = None
    try:
        pending_capture = await _get_pending_capture(current["id"])
    except Exception:
        pending_capture = None

    if pending_capture:
        if unsupported_attachments:
            await message.reply("That attachment type is not supported here. Send images, text, or a voice note.")
            return

        if pending_capture.get("awaiting_description"):
            if _parse_pending_capture_choice(content, pending_capture.get("suggestions", []))[0] == "cancel":
                try:
                    await _cancel_pending_capture(current["id"], pending_capture["capture_id"])
                except Exception as exc:  # noqa: BLE001
                    await message.reply(_extract_error_message(exc, "I could not cancel that pending damage."))
                    return
                await message.reply("Pending damage canceled. You can enter a new damage description now.")
                return

            if message.attachments and image_attachments:
                total_images = pending_capture.get("image_count", 0)
                for attachment in image_attachments:
                    pending_capture = await _add_pending_capture_image(
                        current["id"],
                        pending_capture["capture_id"],
                        attachment,
                    )
                    total_images = pending_capture.get("image_count", total_images)
                if not content and audio_attachment is None:
                    await message.reply(_format_awaiting_description_prompt(total_images))
                    return

            if not content and audio_attachment is None:
                return

            try:
                result = await _describe_pending_capture(
                    current["id"],
                    pending_capture["capture_id"],
                    raw_note=content or None,
                    audio=audio_attachment,
                )
            except Exception as exc:  # noqa: BLE001
                await message.reply(_extract_error_message(exc, "I could not save that damage description."))
                return

            await message.reply(_format_damage_prompt(result))
            return

        if message.attachments:
            if not image_attachments:
                await message.reply("Only image attachments can be added to the current pending damage.")
                return
            total_images = pending_capture.get("image_count", 0)
            for attachment in image_attachments:
                pending_capture = await _add_pending_capture_image(
                    current["id"],
                    pending_capture["capture_id"],
                    attachment,
                )
                total_images = pending_capture.get("image_count", total_images)
            await message.reply(
                f"Added {len(image_attachments)} image(s) to the pending damage. Total images: {total_images}.\n"
                f"Reply with `1`-{len(pending_capture['suggestions'])}`, `0`, `change <category>`, or `cancel damage`."
            )
            return

        choice_status, selection_index, category_key = _parse_pending_capture_choice(
            content, pending_capture["suggestions"]
        )
        if choice_status == "ignore":
            return
        if choice_status == "cancel":
            try:
                await _cancel_pending_capture(current["id"], pending_capture["capture_id"])
            except Exception as exc:  # noqa: BLE001
                await message.reply(_extract_error_message(exc, "I could not cancel that pending damage."))
                return
            await message.reply("Pending damage canceled. You can enter a new damage description now.")
            return
        if choice_status == "manual":
            await message.reply(
                "None of the suggested categories fit.\n\n"
                f"{_format_all_categories()}"
            )
            return
        if choice_status == "invalid":
            await message.reply(
                "That choice did not match any category.\n\n"
                f"{_format_suggestions(pending_capture['suggestions'])}\n\n"
                f"Reply with `1`-{len(pending_capture['suggestions'])}, `0`, or `change <category>`."
            )
            return

        try:
            result = await _confirm_damage(
                current["id"],
                pending_capture["capture_id"],
                selection_index=selection_index,
                category_key=category_key,
            )
        except Exception as exc:  # noqa: BLE001
            await message.reply(_extract_error_message(exc, "I could not save that confirmed damage."))
            return

        confirmed_total = _format_total_cost(
            float(result.get("total_cost", result.get("estimated_cost", 0.0)) or 0.0),
            bool(result.get("chargeable", True)),
        )
        pricing_suffix = f" / {result['pricing_name']}" if result.get("pricing_name") else ""
        await message.reply(
            f"Confirmed and saved under **{result['category']}**{pricing_suffix}.\n"
            f"{result['cleaned_description']} | x{result.get('quantity', 1)} | "
            f"${result.get('unit_cost', 0.0):.2f} each | {confirmed_total}\n"
            "Use `/list_damages` if you need to edit it."
        )
        return

    if current.get("form_fill_status") == "awaiting_approval":
        action = _parse_review_action(content)
        if action == "approve":
            await message.reply(
                f"Starting Microsoft Form fill.\nReview page: {REVIEW_BASE}/api/review/{current['id']}"
            )
            try:
                result = await _complete_session(current["id"])
            except Exception as exc:  # noqa: BLE001
                await message.reply(
                    f"{_extract_error_message(exc, 'I could not fill the live Microsoft Form.')}\n"
                    f"Review page: {REVIEW_BASE}/api/review/{current['id']}"
                )
                return

            await message.reply(
                f"Form fill result: {result['form_fill_status']}.\n{result['message']}\n"
                f"Review page: {REVIEW_BASE}/api/review/{current['id']}"
            )
            await _send_next_ready_schedule_prompt(message.channel, message.author.id, message.channel.id)
            return
        if action == "deny":
            try:
                await _cancel_review(current["id"])
            except Exception as exc:  # noqa: BLE001
                await message.reply(_extract_error_message(exc, "I could not cancel the review state."))
                return
            await message.reply("Review canceled. You can continue editing this checkout.")
            return
        if action == "cancel_checkout":
            try:
                await _cancel_session(current["id"])
            except Exception as exc:  # noqa: BLE001
                await message.reply(_extract_error_message(exc, "I could not cancel this checkout."))
                return
            await message.reply("Checkout canceled. You can start a new one with `/start_checkout`.")
            await _send_next_ready_schedule_prompt(message.channel, message.author.id, message.channel.id)
            return
        if message.attachments:
            await message.reply(
                "This checkout is waiting for review.\n\n"
                f"{_format_review_actions()}"
            )
            return
        await message.reply(
            "This checkout is waiting for review.\n\n"
            f"{_format_review_actions()}"
        )
        return

    if unsupported_attachments:
        await message.reply("That attachment type is not supported for damage reports.")
        return

    if message.attachments and audio_attachment and not image_attachments and not content:
        await message.reply("Send images first, then I can use a text description or voice note for that damage.")
        return

    if not content and not image_attachments:
        return

    if message.attachments and not image_attachments and not audio_attachment:
        await message.reply("Only image attachments can be used to start a damage report.")
        return

    try:
        result = await _capture_damage(current["id"], content, image_attachments)
    except Exception as exc:  # noqa: BLE001
        await message.reply(_extract_error_message(exc, "I could not capture that damage report."))
        return

    if result.get("awaiting_description"):
        await message.reply(_format_awaiting_description_prompt(result.get("image_count", 0)))
        return

    await message.reply(_format_damage_prompt(result))


def run_discord_bot(token: str):
    bot.run(token)
