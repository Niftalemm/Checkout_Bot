# Clawbot Checkout Assistant

Chat-first room checkout assistant with FastAPI, SQLite, Discord commands, local image storage, pricing/matching logic, reminder support, and Microsoft Form draft automation.

## Features

- FastAPI backend with SQLite persistence
- Local filesystem storage for uploaded damage images
- Session workflow with required resident/room fields
- Guided Discord slash-command checkout flow
- Multiple damage items per session
- Fixed JSON pricing sheet and keyword matching engine
- Automatic cleaned description + estimated charge for each damage note
- Session summary command with total estimated cost
- Microsoft Form draft preparation
- Playwright workflow that fills existing form and stops before final submit
- Reminder support from schedule JSON file
- Modular integration layout for future Telegram channel support

## Project structure

```
app/
  api/routes.py
  core/config.py
  db/base.py
  integrations/
    discord/bot.py
    playwright/form_filler.py
  models/entities.py
  reminders/
    service.py
    runner.py
  schemas/session.py
  services/
    checkout_service.py
    form_draft.py
    pricing.py
  storage/image_store.py
data/
  pricing_sheet.json
  schedule.json
uploads/
run_discord_bot.py
requirements.txt
```

## Setup

1. Create and activate a virtual environment:
   - Windows (PowerShell):
     - `python -m venv .venv`
     - `.venv\Scripts\Activate.ps1`
2. Install dependencies:
   - `pip install -r requirements.txt`
3. Install Playwright browser:
   - `python -m playwright install chromium`
4. Create `.env` from template:
   - `copy .env.example .env`
5. Fill `.env` values, especially:
   - `DISCORD_BOT_TOKEN`
- `DISCORD_GUILD_ID`
   - `MICROSOFT_FORM_URL`

## Run with Docker Compose (recommended)

1. Create `.env` from template and fill required values:
   - `copy .env.example .env`
   - Set `DISCORD_BOT_TOKEN` and `MICROSOFT_FORM_URL`
2. Build and start API + Discord bot:
   - `docker compose up --build -d`
3. Optional: start reminders service too:
   - `docker compose --profile reminders up --build -d`
4. View logs:
   - `docker compose logs -f api`
   - `docker compose logs -f discord-bot`
5. Stop services:
   - `docker compose down`

Notes:
- API is available at `http://localhost:8000`
- SQLite DB + uploaded images persist in Docker volume `clawbot_runtime`
- Edit `data/schedule.json` on host; containers read it via bind mount

## Run services

- API:
  - `uvicorn app.main:app --reload`
- Discord bot:
  - `python run_discord_bot.py`
- Reminder loop:
  - `python -m app.reminders.runner`

## API endpoints

- `POST /api/sessions`
  - Body:
    ```json
    {
      "resident_name": "Alex Morgan",
      "room_number": "104B",
      "tech_id": "T-77",
      "hall": "Maple Hall",
      "staff_name": "Jordan Lee",
      "room_side": "Left"
    }
    ```
- `POST /api/sessions/{session_id}/damages`
  - Multipart form fields:
    - `raw_note` (required)
    - `image` (optional)
- `GET /api/sessions/{session_id}/summary`
- `GET /api/sessions/{session_id}/form-draft`
- `POST /api/sessions/{session_id}/form-draft/fill`

## Discord commands

- `/start_checkout`
  - Opens a guided modal for resident name, room number, tech id, hall, staff name, room side.
- During active checkout, send damage as:
  - image attachment + short text caption, or
  - voice attachment + short text note (required by default).
- `/summary`
  - Returns current active session item count + estimated total.
- `/prepare_form [session_id]`
  - Builds form draft only (no browser fill).
- `/fill_form_draft [session_id]`
  - Manually triggers Playwright fill.
- `/complete_checkout`
  - Finalizes active session summary and optionally auto-fills Microsoft Form.

## Pricing and matching

- Pricing rules live in `data/pricing_sheet.json`.
- Matching engine:
  - normalizes note text
  - scores exact phrase hits strongly
  - scores token overlap for fuzzy note matching
  - selects best matching charge item
  - returns:
    - `category`
    - `form_section`
    - `cleaned_description`
    - `estimated_cost`

Example note:
- Input: `"chipped tile near closet"`
- Typical output:
  - category: `Tile - Floor (per square)`
  - default cost: `30.0`

Pricing schema (v2):

```json
{
  "version": 2,
  "items": [
    {
      "name": "Ceiling Tile",
      "form_section": "Ceiling",
      "default_cost": 16.0,
      "keywords": ["ceiling tile", "tile ceiling"]
    }
  ]
}
```

## Microsoft Form automation note

`app/integrations/playwright/form_filler.py` uses generic selectors and intentionally stops before submit. Adjust question labels/selectors to match your specific existing Microsoft Form fields.

## Reminder schedule

- Edit `data/schedule.json` with upcoming checkout timestamps (ISO format).
- Runner checks every 60 seconds and prints reminders for next hour.

## Next extension points

- Add Telegram integration under `app/integrations/telegram/`
- Add auth + permissions
- Add richer NLP cleanup/matching