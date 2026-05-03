# Clawbot Checkout Assistant

Discord-first room checkout assistant with FastAPI, SQLite, private local image storage, persisted checkout state, category suggestion + confirmation, review approval flow, and exact Microsoft Forms automation.

## Features

- FastAPI backend with SQLite persistence
- Private local filesystem storage for uploaded damage images
- Session workflow with required resident/room fields
- Guided Discord slash-command checkout flow
- Persisted pending damage capture before final save
- Category suggestion with numbered choice replies in Discord
- Multiple confirmed damage items per session
- Quantity-aware pricing estimate suggestions
- Session summary and checkout review approval flow in Discord
- Microsoft Form draft preparation for the exact live form
- Playwright workflow that reuses saved Microsoft auth state, fills the live form, and submits automatically in headless Docker runs
- Scheduled checkout reminders from the Discord bot with Start/Edit/Cancel buttons
- Modular integration layout for future Telegram channel support

## Project structure

```
app/
  api/routes.py
  core/config.py
  db/base.py
  integrations/
    discord/bot.py
    playwright/auth_session.py
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
  form_mapping.json
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
   - `PLAYWRIGHT_STORAGE_STATE_PATH`
6. Authenticate once for the live Microsoft Form session:
   - `python -m app.integrations.playwright.auth_session`

## Run with Docker Compose (recommended)

1. Create `.env` from template and fill required values:
   - `copy .env.example .env`
   - Set `DISCORD_BOT_TOKEN` and `MICROSOFT_FORM_URL`
2. Authenticate once for the live Microsoft Form session from your host machine:
   - `python -m app.integrations.playwright.auth_session`
   - Sign in to Microsoft in the browser window, open the live form, then press Enter in the terminal.
   - This creates `runtime/playwright/storage_state.json`, which Docker mounts into the app container.
3. Build and start API + Discord bot:
   - `docker compose up --build -d`
4. View logs:
   - `docker compose logs -f api`
   - `docker compose logs -f discord-bot`
5. Stop services:
   - `docker compose down`

Notes:
- API is available at `http://localhost:8000`
- SQLite DB + uploaded images persist in Docker volume `clawbot_runtime`
- Docker Compose runs Playwright with `PLAYWRIGHT_HEADLESS=true` and `PLAYWRIGHT_AUTO_SUBMIT_HEADLESS=true` because containers do not have a desktop display. Use the non-Docker local run with `PLAYWRIGHT_HEADLESS=false` if you need the filled Microsoft Form to stay open for manual review before submit.
- The Discord bot polls scheduled checkouts and posts due reminders automatically.
- The separate reminders profile is still available for legacy/manual runs with `STANDALONE_REMINDERS_ENABLED=true`, but normal use only needs API + Discord bot.

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
      "hall": "A",
      "staff_name": "Nift",
      "room_side": "left"
    }
    ```
- `POST /api/sessions/{session_id}/damage-captures`
  - Multipart form fields:
    - `raw_note` (required)
    - `image` (required)
- `GET /api/sessions/{session_id}/pending-capture`
- `POST /api/sessions/{session_id}/damage-captures/{capture_id}/confirm`
- `GET /api/sessions/{session_id}/summary`
- `POST /api/sessions/{session_id}/review`
- `POST /api/sessions/{session_id}/review/cancel`
- `POST /api/sessions/{session_id}/cancel`
- `GET /api/sessions/{session_id}/form-draft`
- `POST /api/sessions/{session_id}/form-draft/fill`
- `POST /api/sessions/{session_id}/complete`

## Discord commands

- `/start_checkout`
  - Starts checkout setup and asks for a follow-up message with one field per line:
    - resident name
    - room number
    - tech id
    - hall (`A`, `B`, `C`, or `D`)
    - room side
  - Staff name defaults to `Nift`
- During active checkout, send damage as:
  - image attachment + required short text description
  - bot suggests a category
  - reply with the number of the category you want to save
- `/summary`
  - Returns confirmed item count + estimated total.
- `/prepare_form [session_id]`
  - Builds form draft only (no browser fill).
- `/review_page [session_id]`
  - Sends the lightweight web UI link for review, scheduling, damage edits, and Microsoft Form fill status.
- `/fill_form_draft [session_id]`
  - Retries the live Playwright fill for a session.
- `/complete_checkout`
  - Sends a Discord review summary and waits for approval.
  - Reply `1` to approve and fill the live Microsoft Form.
  - Reply `2` to deny and keep editing.
  - Reply `3` to cancel the checkout.
- `/cancel_checkout`
  - Cancels the active checkout in the current channel.
- `/schedule_checkout`
  - Opens a pop-up form to schedule a future checkout in Central time.
  - Use `Hall|Side` like `A|left`.
  - Use date/time like `2026-05-01 14:30`.
- `/my_schedule`
  - Shows your upcoming scheduled checkouts with Start, Edit, and Cancel buttons.
- `/edit_scheduled_checkout`
  - Edits a scheduled checkout by ID when you want to update fields from the command line.
- `/cancel_scheduled_checkout`
  - Cancels a scheduled checkout by ID.

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

## Discord damage flow

1. Start a checkout with `/start_checkout`.
2. Send resident details in the requested 5-line format.
3. For each damage:
   - send an image and short text description in the same message
   - bot suggests the most likely category
   - reply with the number of the category you want
4. Run `/complete_checkout` to get a review summary in Discord.
5. Reply `1` to fill the live Microsoft Form.
6. In Docker/headless mode, Clawbot submits the live Microsoft Form after a clean fill. In local headed mode, review the opened browser window and click Submit manually.

## Microsoft auth session

- The live form fill reuses Playwright storage state from `PLAYWRIGHT_STORAGE_STATE_PATH`.
- Authenticate once by running:
  - `python -m app.integrations.playwright.auth_session`
- If you run the app with Docker Compose, run this command on your host machine, not inside the container. The browser login is saved to `runtime/playwright/storage_state.json`, and Docker mounts that folder at `/app/runtime/playwright`.
- Docker Compose fills and submits forms in headless mode to avoid the container X server error. Headed/manual browser review needs a local non-Docker API run or a container display setup such as Xvfb/VNC.
- The auth state file should stay in a private gitignored path such as `runtime/playwright/storage_state.json`.

## Hosting and security notes

- Keep secrets in `.env` only.
- `DISCORD_BOT_TOKEN` must never be committed.
- Playwright auth/session state must stay in a private gitignored path.
- Uploaded images are stored locally only and are not exposed through public API routes by default.
- This project is best suited for local or internal deployment while the live browser review step is manual.

## Manual test checklist

1. Start the API and Discord bot.
2. Run `python -m app.integrations.playwright.auth_session` and save Microsoft storage state.
3. Start a Discord checkout and enter resident details.
4. Send an image without text and confirm it is rejected.
5. Send text without an image and confirm it is rejected.
6. Send an image with a description and confirm the bot suggests a category instead of saving immediately.
7. Reply with a numbered category choice and confirm the damage is only saved after confirmation.
8. Add a second confirmed damage and run `/summary`.
9. Run `/complete_checkout` and confirm the Discord review summary appears.
10. Reply `2`, then add another damage, and confirm editing still works.
11. Run `/complete_checkout` again and reply `1`.
12. Start another checkout, use `/cancel_checkout`, and confirm the active session is removed.
13. Verify the live Microsoft Form fills the exact sections and records a submit result.
14. Close the browser and confirm the session records `success`, `partial_failure`, or `failed` cleanly instead of crashing.

## Scheduled checkout reminders

- Create scheduled checkouts with `/schedule_checkout` or from the review page UI.
- The Discord bot checks for due reminders about every 45 seconds.
- At checkout time, the bot posts the scheduled checkout card in the original Discord channel with Start, Edit, and Cancel buttons.

## Lightweight UI

- Open `/api/review/{session_id}` from `/review_page`.
- The page shows session details, damage edits, schedule controls, Microsoft Form fill status, and retry/open-form actions.

## Next extension points

- Add Telegram integration under `app/integrations/telegram/`
- Add auth + permissions
- Add richer NLP cleanup/matching
