import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import get_db
from app.integrations.playwright.form_filler import MicrosoftFormFiller
from app.schemas.session import (
    CompleteSessionResponse,
    DamageConfirmRequest,
    DamageItemUpdateCategoryRequest,
    DamageItemUpdateDescriptionRequest,
    DamageResponse,
    DiscordSessionStart,
    FormDraft,
    PendingCaptureState,
    PendingDamageCaptureResponse,
    ReviewSummary,
    ScheduledCheckoutCancelRequest,
    ScheduledCheckoutCreateRequest,
    ScheduledCheckoutResponse,
    ScheduledCheckoutStartRequest,
    ScheduledCheckoutStartResponse,
    ScheduledCheckoutUpdateRequest,
    SessionCreate,
    SessionDetailsUpdate,
    SessionResponse,
    SessionSummary,
)
from app.services.checkout_service import (
    FORM_FILL_AWAITING_APPROVAL,
    FORM_FILL_PARTIAL_FAILURE,
    STATUS_COMPLETE_FAILED,
    CheckoutService,
    ServiceError,
)
from app.services.damage_ai import DamageAIService
from app.services.form_draft import FormDraftService
from app.services.form_mapping import get_damage_sections
from app.services.pricing import PricingEngine
from app.services.schedule_service import ScheduleService
from app.storage.image_store import LocalImageStore

router = APIRouter(prefix="/api", tags=["checkout"])


def build_service(db: Session) -> CheckoutService:
    pricing = PricingEngine(settings.pricing_sheet_path)
    store = LocalImageStore(settings.uploads_dir)
    ai_service = DamageAIService(
        api_key=settings.groq_api_key,
        base_url=settings.groq_base_url,
        model=settings.groq_model,
        timeout_seconds=settings.groq_timeout_seconds,
        transcription_model=settings.groq_transcription_model,
    )
    return CheckoutService(db=db, pricing=pricing, image_store=store, ai_service=ai_service)


def _raise_http_error(exc: ServiceError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


def _parse_result(payload: str | None) -> dict | None:
    if not payload:
        return None
    return json.loads(payload)


def build_schedule_service(db: Session) -> ScheduleService:
    return ScheduleService(db=db)


def _schedule_lead(reminder_field: str, active_exists: bool) -> str:
    if reminder_field == "reminder_30_sent":
        return "Reminder: checkout in 30 minutes."
    if reminder_field == "reminder_10_sent":
        return "Reminder: checkout in 10 minutes."
    if active_exists:
        return "Checkout time is now. Finish the current checkout first, then start this scheduled one."
    return "Checkout time is now. You can start this scheduled checkout below."


@router.post("/sessions", response_model=SessionResponse)
def create_session(payload: SessionCreate, db: Session = Depends(get_db)):
    service = build_service(db)
    try:
        return service.create_session(payload)
    except ServiceError as exc:
        _raise_http_error(exc)


@router.post("/sessions/discord/start", response_model=SessionResponse)
def start_discord_session(payload: DiscordSessionStart, db: Session = Depends(get_db)):
    service = build_service(db)
    try:
        return service.start_discord_session(payload)
    except ServiceError as exc:
        _raise_http_error(exc)


@router.post("/scheduled-checkouts", response_model=ScheduledCheckoutResponse)
def create_scheduled_checkout(payload: ScheduledCheckoutCreateRequest, db: Session = Depends(get_db)):
    service = build_schedule_service(db)
    try:
        return service.create_scheduled_checkout(payload)
    except ServiceError as exc:
        _raise_http_error(exc)


@router.get("/scheduled-checkouts", response_model=list[ScheduledCheckoutResponse])
def list_scheduled_checkouts(
    creator_discord_user_id: str = Query(...),
    include_terminal: bool = Query(False),
    db: Session = Depends(get_db),
):
    service = build_schedule_service(db)
    try:
        return service.list_scheduled_checkouts(creator_discord_user_id, include_terminal=include_terminal)
    except ServiceError as exc:
        _raise_http_error(exc)


@router.get("/scheduled-checkouts-due-reminders")
def list_due_scheduled_checkout_reminders(db: Session = Depends(get_db)):
    service = build_schedule_service(db)
    try:
        return [
            {
                "schedule": ScheduledCheckoutResponse.model_validate(schedule).model_dump(mode="json"),
                "reminder_field": reminder_field,
                "lead": _schedule_lead(reminder_field, active_exists),
            }
            for schedule, reminder_field, active_exists in service.list_due_reminders(datetime.now(UTC))
        ]
    except ServiceError as exc:
        _raise_http_error(exc)


@router.post("/scheduled-checkouts/{schedule_id}/mark-reminder-sent", response_model=ScheduledCheckoutResponse)
def mark_scheduled_checkout_reminder_sent(
    schedule_id: str,
    reminder_field: str = Query(...),
    db: Session = Depends(get_db),
):
    service = build_schedule_service(db)
    try:
        return service.mark_reminder_sent(schedule_id, reminder_field)
    except ServiceError as exc:
        _raise_http_error(exc)


@router.get("/scheduled-checkouts/{schedule_id}", response_model=ScheduledCheckoutResponse)
def get_scheduled_checkout(
    schedule_id: str,
    creator_discord_user_id: str = Query(...),
    db: Session = Depends(get_db),
):
    service = build_schedule_service(db)
    try:
        return service.get_scheduled_checkout(schedule_id, creator_discord_user_id)
    except ServiceError as exc:
        _raise_http_error(exc)


@router.put("/scheduled-checkouts/{schedule_id}", response_model=ScheduledCheckoutResponse)
def update_scheduled_checkout(
    schedule_id: str,
    payload: ScheduledCheckoutUpdateRequest,
    db: Session = Depends(get_db),
):
    service = build_schedule_service(db)
    try:
        return service.update_scheduled_checkout(schedule_id, payload)
    except ServiceError as exc:
        _raise_http_error(exc)


@router.post("/scheduled-checkouts/{schedule_id}/cancel", response_model=ScheduledCheckoutResponse)
def cancel_scheduled_checkout(
    schedule_id: str,
    payload: ScheduledCheckoutCancelRequest,
    db: Session = Depends(get_db),
):
    service = build_schedule_service(db)
    try:
        return service.cancel_scheduled_checkout(schedule_id, payload.creator_discord_user_id)
    except ServiceError as exc:
        _raise_http_error(exc)


@router.post("/scheduled-checkouts/{schedule_id}/start", response_model=ScheduledCheckoutStartResponse)
def start_scheduled_checkout(
    schedule_id: str,
    payload: ScheduledCheckoutStartRequest,
    db: Session = Depends(get_db),
):
    service = build_schedule_service(db)
    try:
        return service.start_scheduled_checkout(schedule_id, payload)
    except ServiceError as exc:
        _raise_http_error(exc)


@router.get("/scheduled-checkouts-ready-next", response_model=ScheduledCheckoutResponse | None)
def next_ready_checkout(
    creator_discord_user_id: str = Query(...),
    discord_channel_id: str = Query(...),
    only_unnotified: bool = Query(False),
    db: Session = Depends(get_db),
):
    service = build_schedule_service(db)
    schedule = service.get_next_ready_checkout(
        creator_discord_user_id,
        discord_channel_id,
        only_unnotified=only_unnotified,
    )
    return schedule


@router.post("/scheduled-checkouts/{schedule_id}/mark-ready-notified", response_model=ScheduledCheckoutResponse)
def mark_ready_checkout_notified(schedule_id: str, db: Session = Depends(get_db)):
    service = build_schedule_service(db)
    try:
        return service.mark_ready_to_start_notified(schedule_id)
    except ServiceError as exc:
        _raise_http_error(exc)


@router.get("/sessions/active", response_model=SessionResponse)
def get_active_session(channel_id: int = Query(...), db: Session = Depends(get_db)):
    service = build_service(db)
    session = service.get_channel_session(channel_id)
    if not session:
        raise HTTPException(status_code=404, detail="No active checkout in this channel.")
    return session


@router.get("/sessions/{session_id}", response_model=SessionResponse)
def get_session(session_id: int, db: Session = Depends(get_db)):
    service = build_service(db)
    session = service.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")
    return session


def _build_review_state(session_id: int, db: Session) -> dict:
    service = build_service(db)
    schedule_service = build_schedule_service(db)
    summary = service.summarize_session(session_id)
    session = service.get_session(session_id)
    if not session:
        raise ServiceError("Session not found.", status_code=404)
    schedules = []
    if session.started_by:
        schedules = [
            ScheduledCheckoutResponse.model_validate(schedule).model_dump(mode="json")
            for schedule in schedule_service.list_scheduled_checkouts(session.started_by)
        ]
    category_options = [
        {"key": section["key"], "name": section["name"]}
        for section in get_damage_sections()
    ]
    return {
        "session": summary.model_dump(mode="json"),
        "session_details": SessionResponse.model_validate(session).model_dump(mode="json"),
        "categories": category_options,
        "schedules": schedules,
        "review_path": f"/api/review/{session_id}",
        "microsoft_form_url": settings.microsoft_form_url,
        "form_fill_result": _parse_result(session.form_fill_result),
    }


@router.put("/sessions/{session_id}/details", response_model=SessionResponse)
def update_session_details(
    session_id: int, payload: SessionDetailsUpdate, db: Session = Depends(get_db)
):
    service = build_service(db)
    try:
        return service.update_session_details(session_id, payload)
    except ServiceError as exc:
        _raise_http_error(exc)


@router.post("/sessions/{session_id}/damage-captures", response_model=PendingDamageCaptureResponse)
def capture_damage(
    session_id: int,
    raw_note: str | None = Form(None),
    image: UploadFile | None = File(None),
    images: list[UploadFile] | None = File(None),
    db: Session = Depends(get_db),
):
    service = build_service(db)
    uploads = list(images or [])
    if image is not None:
        uploads.insert(0, image)
    try:
        return service.capture_damage(
            session_id=session_id,
            raw_note=raw_note or "",
            image_files=uploads,
            image_name_hints=[upload.filename or "" for upload in uploads],
        )
    except ServiceError as exc:
        _raise_http_error(exc)


@router.get("/sessions/{session_id}/pending-capture", response_model=PendingCaptureState)
def get_pending_capture(session_id: int, db: Session = Depends(get_db)):
    service = build_service(db)
    capture = service.get_pending_capture(session_id)
    if not capture:
        raise HTTPException(status_code=404, detail="No pending damage capture for this session.")
    return service._pending_capture_state(capture)


@router.post(
    "/sessions/{session_id}/damage-captures/{capture_id}/images",
    response_model=PendingCaptureState,
)
def add_pending_capture_image(
    session_id: int,
    capture_id: int,
    image: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    service = build_service(db)
    try:
        return service.add_pending_capture_image(session_id, capture_id, image)
    except ServiceError as exc:
        _raise_http_error(exc)


@router.post(
    "/sessions/{session_id}/damage-captures/{capture_id}/description",
    response_model=PendingDamageCaptureResponse,
)
def describe_pending_capture(
    session_id: int,
    capture_id: int,
    raw_note: str | None = Form(None),
    audio: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    service = build_service(db)
    try:
        return service.describe_pending_capture(
            session_id=session_id,
            capture_id=capture_id,
            raw_note=raw_note or "",
            audio_file=audio,
        )
    except ServiceError as exc:
        _raise_http_error(exc)


@router.post("/sessions/{session_id}/damage-captures/{capture_id}/cancel")
def cancel_pending_capture(
    session_id: int,
    capture_id: int,
    db: Session = Depends(get_db),
):
    service = build_service(db)
    try:
        service.cancel_pending_capture(session_id, capture_id)
    except ServiceError as exc:
        _raise_http_error(exc)
    return {"message": "Pending damage canceled."}


@router.post("/sessions/{session_id}/damage-captures/{capture_id}/confirm")
def confirm_damage(
    session_id: int,
    capture_id: int,
    payload: DamageConfirmRequest,
    db: Session = Depends(get_db),
):
    service = build_service(db)
    try:
        damage = service.confirm_damage_capture(
            session_id,
            capture_id,
            selection_index=payload.selection_index,
            category_key=payload.category_key,
        )
    except ServiceError as exc:
        _raise_http_error(exc)
    return {
        "message": "Damage saved.",
        "category": damage.category,
        "pricing_name": damage.pricing_name,
        "cleaned_description": damage.cleaned_description,
        "quantity": damage.quantity,
        "unit_cost": damage.unit_cost,
        "total_cost": damage.total_cost,
        "chargeable": damage.chargeable,
        "estimated_cost": damage.estimated_cost,
        "confirmation_status": damage.confirmation_status,
    }


@router.get("/sessions/{session_id}/summary", response_model=SessionSummary)
def session_summary(session_id: int, db: Session = Depends(get_db)):
    service = build_service(db)
    try:
        return service.summarize_session(session_id)
    except ServiceError as exc:
        _raise_http_error(exc)


@router.get("/sessions/{session_id}/damage-items", response_model=list[DamageResponse])
def list_damage_items(session_id: int, db: Session = Depends(get_db)):
    service = build_service(db)
    try:
        return service.list_damage_items(session_id)
    except ServiceError as exc:
        _raise_http_error(exc)


@router.get("/review/{session_id}", response_class=HTMLResponse)
def review_page(session_id: int, db: Session = Depends(get_db)):
    try:
        payload = _build_review_state(session_id, db)
    except ServiceError as exc:
        _raise_http_error(exc)

    app_payload = json.dumps(payload).replace("</", "<\\/")
    html = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Clawbot Checkout __SESSION_ID__</title>
  <style>
    :root {
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #1e2430;
      --muted: #647083;
      --accent: #2f6f73;
      --accent-2: #4958b8;
      --danger: #b83f3f;
      --border: #d8dee8;
    }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: Inter, ui-sans-serif, system-ui, "Segoe UI", sans-serif; background: var(--bg); color: var(--ink); }
    main { max-width: 1180px; margin: 0 auto; padding: 18px; }
    header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 16px; }
    h1, h2, h3 { margin: 0; }
    h1 { font-size: 28px; }
    h2 { font-size: 18px; }
    .muted { color: var(--muted); }
    .grid { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(340px, 0.9fr); gap: 14px; }
    .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 14px; box-shadow: 0 8px 22px rgba(30, 36, 48, 0.06); }
    .meta { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-top: 12px; }
    .stat { border: 1px solid var(--border); border-radius: 8px; padding: 10px; min-height: 64px; }
    .stat span { color: var(--muted); display: block; font-size: 12px; margin-bottom: 4px; }
    .toolbar { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
    .item, .schedule { border-top: 1px solid var(--border); padding-top: 12px; margin-top: 12px; }
    .item-head, .schedule-head { display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; }
    label { display: block; color: var(--muted); font-size: 12px; margin-top: 10px; }
    input, textarea, select { width: 100%; padding: 10px; border-radius: 8px; border: 1px solid var(--border); background: white; font: inherit; margin-top: 4px; }
    textarea { min-height: 84px; resize: vertical; }
    button { border: 0; border-radius: 8px; padding: 10px 14px; background: var(--accent); color: white; font: inherit; font-weight: 650; cursor: pointer; }
    button.secondary { background: var(--accent-2); }
    button.ghost { background: #eef2f7; color: var(--ink); }
    button.danger { background: var(--danger); }
    .thumbs { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0; }
    .thumb { border: 1px solid var(--border); border-radius: 8px; padding: 8px; background: #f9fafc; font-size: 13px; }
    .status { font-size: 14px; color: var(--accent); margin-top: 8px; min-height: 20px; }
    .split { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .form-result { white-space: pre-wrap; background: #f9fafc; border: 1px solid var(--border); border-radius: 8px; padding: 10px; max-height: 260px; overflow: auto; font-size: 13px; }
    @media (max-width: 880px) { .grid, .meta, .split { grid-template-columns: 1fr; } header { display: block; } }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Clawbot Checkout</h1>
        <div class="muted">Review, schedule, and fill the Microsoft Form from one place.</div>
      </div>
      <button class="ghost" onclick="reloadState()">Refresh</button>
    </header>
    <section class="panel">
      <h2>Session</h2>
      <div class="meta">
        <div class="stat"><span>Session</span><strong id="session-id"></strong></div>
        <div class="stat"><span>Resident</span><strong id="resident-name"></strong></div>
        <div class="stat"><span>Room</span><strong id="room-number"></strong></div>
        <div class="stat"><span>Total</span><strong>$<span id="total-cost"></span></strong></div>
      </div>
      <div class="toolbar">
        <button onclick="prepareDraft()">Prepare Draft</button>
        <button class="secondary" onclick="fillForm()">Fill & Submit Microsoft Form</button>
        <button class="ghost" onclick="openMicrosoftForm()">Open Live Form</button>
      </div>
      <div class="status" id="page-status"></div>
    </section>
    <div class="grid">
      <section class="panel">
        <h2>Damage Items</h2>
        <div id="items"></div>
      </section>
      <aside>
        <section class="panel">
          <h2>Scheduled Checkouts</h2>
          <div id="schedules"></div>
        </section>
        <section class="panel">
          <h2>Create Schedule</h2>
          <div class="split">
            <label>Resident<input id="new-resident" autocomplete="off"></label>
            <label>Room<input id="new-room" autocomplete="off"></label>
            <label>TechID<input id="new-tech" autocomplete="off"></label>
            <label>Hall|Side<input id="new-hall-side" placeholder="A|left" autocomplete="off"></label>
          </div>
          <label>YYYY-MM-DD HH:MM Central<input id="new-time" placeholder="2026-05-01 14:30" autocomplete="off"></label>
          <div class="toolbar"><button onclick="createSchedule()">Create Schedule</button></div>
        </section>
        <section class="panel">
          <h2>Form Fill Result</h2>
          <div class="form-result" id="form-result">No result yet.</div>
        </section>
      </aside>
    </div>
  </main>
  <script>
    let state = __APP_PAYLOAD__;
    const sessionId = __SESSION_ID__;

    function categoryOptions() {
      return state.categories.map((category) => `<option value="${category.key}">${category.name}</option>`).join("");
    }
    function setStatus(message) {
      document.getElementById("page-status").textContent = message || "";
    }
    function escapeHtml(value) {
      return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
    }
    function render(nextState) {
      state = nextState;
      const session = state.session;
      const details = state.session_details;
      document.getElementById("session-id").textContent = session.session_id;
      document.getElementById("resident-name").textContent = session.resident_name;
      document.getElementById("room-number").textContent = `${session.room_number} / ${session.hall} / ${session.room_side}`;
      document.getElementById("total-cost").textContent = Number(session.total_estimated_cost).toFixed(2);
      document.getElementById("new-resident").value ||= session.resident_name || "";
      document.getElementById("new-room").value ||= session.room_number || "";
      document.getElementById("new-tech").value ||= session.tech_id || "";
      document.getElementById("new-hall-side").value ||= `${session.hall}|${session.room_side}`;
      renderItems(session.items || []);
      renderSchedules(state.schedules || []);
      document.getElementById("form-result").textContent = state.form_fill_result
        ? JSON.stringify(state.form_fill_result, null, 2)
        : `${details.form_fill_status}${details.form_fill_error ? `\n${details.form_fill_error}` : ""}`;
    }
    function renderItems(items) {
      document.getElementById("items").innerHTML = items.map((item) => `
        <div class="item">
          <div class="item-head">
            <div><strong>ID ${item.id}: ${escapeHtml(item.category)}</strong><br><span class="muted">$${Number(item.estimated_cost).toFixed(2)}</span></div>
            <button class="danger" onclick="deleteItem(${item.id})">Delete</button>
          </div>
          <label>Description<textarea id="desc-${item.id}">${escapeHtml(item.raw_note)}</textarea></label>
          <label>Category<select id="cat-${item.id}">${categoryOptions()}</select></label>
          <div class="thumbs">
            ${(item.images || []).map((image) => `
              <div class="thumb">
                Image ${image.id}${image.is_primary ? " (primary)" : ""}<br>
                <button class="danger" onclick="removeImage(${item.id}, ${image.id})">Remove</button>
              </div>
            `).join("") || "No images yet."}
          </div>
          <input type="file" id="file-${item.id}" accept="image/*">
          <div class="toolbar">
            <button onclick="saveDescription(${item.id})">Save Description</button>
            <button class="secondary" onclick="saveCategory(${item.id})">Save Category</button>
            <button class="ghost" onclick="addImage(${item.id})">Add Image</button>
          </div>
        </div>
      `).join("");
      items.forEach((item) => {
        const select = document.getElementById(`cat-${item.id}`);
        if (select) select.value = item.form_section;
      });
    }
    function renderSchedules(schedules) {
      document.getElementById("schedules").innerHTML = schedules.length ? schedules.map((schedule, index) => `
        <div class="schedule">
          <strong>${escapeHtml(schedule.resident_name)}</strong>
          <div class="muted">Room ${escapeHtml(schedule.room_number)} / ${escapeHtml(schedule.hall)} / ${escapeHtml(schedule.room_side)}</div>
          <div>${new Date(schedule.checkout_time).toLocaleString()}</div>
          <div class="muted">${escapeHtml(schedule.status)} / ${escapeHtml(schedule.id)}</div>
          <div class="toolbar">
            <button onclick="startSchedule(${index})">Start</button>
            <button class="danger" onclick="cancelSchedule(${index})">Cancel</button>
          </div>
        </div>
      `).join("") : "No upcoming schedules.";
    }
    async function reloadState() {
      const response = await fetch(`/api/review/${sessionId}/state`);
      const data = await response.json();
      render(data);
      setStatus("Review refreshed.");
    }
    async function saveDescription(itemId) {
      const raw_note = document.getElementById(`desc-${itemId}`).value;
      const response = await fetch(`/api/sessions/${sessionId}/damage-items/${itemId}/description`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ raw_note })
      });
      if (!response.ok) {
        setStatus("Description update failed.");
        return;
      }
      await reloadState();
    }
    async function saveCategory(itemId) {
      const category_key = document.getElementById(`cat-${itemId}`).value;
      const response = await fetch(`/api/sessions/${sessionId}/damage-items/${itemId}/category`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ category_key })
      });
      if (!response.ok) {
        setStatus("Category update failed.");
        return;
      }
      await reloadState();
    }
    async function deleteItem(itemId) {
      const response = await fetch(`/api/sessions/${sessionId}/damage-items/${itemId}`, { method: "DELETE" });
      if (!response.ok) {
        setStatus("Delete failed.");
        return;
      }
      await reloadState();
    }
    async function addImage(itemId) {
      const fileInput = document.getElementById(`file-${itemId}`);
      if (!fileInput.files.length) {
        setStatus("Choose an image first.");
        return;
      }
      const formData = new FormData();
      formData.append("image", fileInput.files[0]);
      const response = await fetch(`/api/sessions/${sessionId}/damage-items/${itemId}/images`, {
        method: "POST",
        body: formData
      });
      if (!response.ok) {
        setStatus("Image upload failed.");
        return;
      }
      await reloadState();
    }
    async function removeImage(itemId, imageId) {
      const response = await fetch(`/api/sessions/${sessionId}/damage-items/${itemId}/images/${imageId}`, { method: "DELETE" });
      if (!response.ok) {
        setStatus("Image removal failed.");
        return;
      }
      await reloadState();
    }
    async function prepareDraft() {
      const response = await fetch(`/api/sessions/${sessionId}/form-draft`);
      const data = await response.json();
      setStatus(response.ok ? `Draft ready with ${data.sections.filter((section) => section.answer_yes_no === "Yes").length} damage section(s).` : data.detail);
    }
    async function fillForm() {
      setStatus("Filling and submitting the live Microsoft Form...");
      const reviewResponse = await fetch(`/api/sessions/${sessionId}/review`, { method: "POST" });
      if (!reviewResponse.ok) {
        setStatus("Review check failed. Resolve any pending item first.");
        return;
      }
      const response = await fetch(`/api/sessions/${sessionId}/complete`, { method: "POST" });
      const data = await response.json();
      setStatus(data.message || "Form fill finished.");
      await reloadState();
    }
    function openMicrosoftForm() {
      setStatus("Opening the live Microsoft Form in this browser. Headless automation runs separately and is not visible here.");
      if (state.microsoft_form_url) window.open(state.microsoft_form_url, "_blank", "noopener");
    }
    async function createSchedule() {
      const [hall, room_side] = document.getElementById("new-hall-side").value.split("|").map((part) => part.trim());
      const [checkout_date, checkout_time] = document.getElementById("new-time").value.split(/\\s+/, 2);
      const details = state.session_details;
      const payload = {
        resident_name: document.getElementById("new-resident").value,
        room_number: document.getElementById("new-room").value,
        tech_id: document.getElementById("new-tech").value,
        hall,
        room_side,
        checkout_date,
        checkout_time,
        creator_discord_user_id: details.started_by || "web",
        creator_display_name: details.staff_name || "Web",
        discord_channel_id: details.channel_id || "web"
      };
      const response = await fetch("/api/scheduled-checkouts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await response.json();
      setStatus(response.ok ? "Schedule created." : data.detail);
      if (response.ok) await reloadState();
    }
    async function startSchedule(index) {
      const schedule = state.schedules[index];
      const response = await fetch(`/api/scheduled-checkouts/${schedule.id}/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          creator_discord_user_id: schedule.creator_discord_user_id,
          creator_display_name: schedule.creator_display_name,
          discord_channel_id: schedule.discord_channel_id
        })
      });
      const data = await response.json();
      setStatus(data.message || (response.ok ? "Schedule started." : data.detail));
      if (response.ok) await reloadState();
    }
    async function cancelSchedule(index) {
      const schedule = state.schedules[index];
      const response = await fetch(`/api/scheduled-checkouts/${schedule.id}/cancel`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ creator_discord_user_id: schedule.creator_discord_user_id })
      });
      const data = await response.json();
      setStatus(response.ok ? "Schedule canceled." : data.detail);
      if (response.ok) await reloadState();
    }
    render(state);
  </script>
</body>
</html>
    """
    return HTMLResponse(html.replace("__SESSION_ID__", str(session_id)).replace("__APP_PAYLOAD__", app_payload))


@router.get("/review/{session_id}/state")
def review_state(session_id: int, db: Session = Depends(get_db)):
    try:
        return _build_review_state(session_id, db)
    except ServiceError as exc:
        _raise_http_error(exc)


@router.put("/sessions/{session_id}/damage-items/{item_id}/description", response_model=DamageResponse)
def update_damage_description(
    session_id: int,
    item_id: int,
    payload: DamageItemUpdateDescriptionRequest,
    db: Session = Depends(get_db),
):
    service = build_service(db)
    try:
        return service.update_damage_description(session_id, item_id, payload.raw_note)
    except ServiceError as exc:
        _raise_http_error(exc)


@router.put("/sessions/{session_id}/damage-items/{item_id}/category", response_model=DamageResponse)
def update_damage_category(
    session_id: int,
    item_id: int,
    payload: DamageItemUpdateCategoryRequest,
    db: Session = Depends(get_db),
):
    service = build_service(db)
    try:
        return service.update_damage_category(session_id, item_id, payload.category_key)
    except ServiceError as exc:
        _raise_http_error(exc)


@router.delete("/sessions/{session_id}/damage-items/{item_id}")
def delete_damage_item(session_id: int, item_id: int, db: Session = Depends(get_db)):
    service = build_service(db)
    try:
        service.delete_damage_item(session_id, item_id)
    except ServiceError as exc:
        _raise_http_error(exc)
    return {"message": "Damage item deleted."}


@router.post("/sessions/{session_id}/damage-items/{item_id}/images", response_model=DamageResponse)
def add_damage_item_image(
    session_id: int,
    item_id: int,
    image: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    service = build_service(db)
    try:
        return service.add_damage_item_image(session_id, item_id, image)
    except ServiceError as exc:
        _raise_http_error(exc)


@router.delete(
    "/sessions/{session_id}/damage-items/{item_id}/images/{image_id}",
    response_model=DamageResponse,
)
def remove_damage_item_image(
    session_id: int,
    item_id: int,
    image_id: int,
    db: Session = Depends(get_db),
):
    service = build_service(db)
    try:
        return service.remove_damage_item_image(session_id, item_id, image_id)
    except ServiceError as exc:
        _raise_http_error(exc)


@router.post("/sessions/{session_id}/review", response_model=ReviewSummary)
def request_review(session_id: int, db: Session = Depends(get_db)):
    service = build_service(db)
    try:
        service.request_review(session_id)
        return service.build_review_summary(session_id)
    except ServiceError as exc:
        _raise_http_error(exc)


@router.post("/sessions/{session_id}/review/cancel", response_model=SessionResponse)
def cancel_review(session_id: int, db: Session = Depends(get_db)):
    service = build_service(db)
    try:
        return service.cancel_review(session_id)
    except ServiceError as exc:
        _raise_http_error(exc)


@router.post("/sessions/{session_id}/cancel")
def cancel_session(session_id: int, db: Session = Depends(get_db)):
    service = build_service(db)
    schedule_service = build_schedule_service(db)
    try:
        service.cancel_session(session_id)
        schedule_service.mark_linked_schedule_canceled(session_id)
    except ServiceError as exc:
        _raise_http_error(exc)
    return {"message": "Checkout canceled."}


@router.get("/sessions/{session_id}/form-draft", response_model=FormDraft)
def prepare_form_draft(session_id: int, db: Session = Depends(get_db)):
    service = build_service(db)
    draft_service = FormDraftService(service)
    try:
        return draft_service.build_draft(session_id)
    except ServiceError as exc:
        _raise_http_error(exc)


@router.post("/sessions/{session_id}/form-draft/fill")
def fill_form_draft(session_id: int, db: Session = Depends(get_db)):
    service = build_service(db)
    draft_service = FormDraftService(service)
    try:
        session = service.get_session(session_id)
        if not session:
            raise ServiceError("Session not found.", status_code=404)
        draft = draft_service.build_draft(session_id)
        service.mark_form_fill_pending(session_id)
        filler = MicrosoftFormFiller(settings.microsoft_form_url)
        outcome = filler.fill_draft(draft)
        if outcome["status"] == FORM_FILL_PARTIAL_FAILURE:
            updated = service.mark_form_fill_partial_failure(
                session_id,
                outcome,
                "Form fill reached the review screen, but some conditional fields need manual attention.",
            )
        else:
            updated = service.mark_form_fill_success(
                session_id,
                outcome,
                mark_completed=session.status == STATUS_COMPLETE_FAILED,
            )
    except ServiceError as exc:
        _raise_http_error(exc)
    except Exception as exc:
        error_message = f"Automatic form fill failed. {exc}"
        updated = service.mark_form_fill_failure(
            session_id,
            error_message,
            result={"error": str(exc)},
        )
        return {
            "message": error_message,
            "form_fill_status": updated.form_fill_status,
            "status": updated.status,
            "form_fill_result": _parse_result(updated.form_fill_result),
        }
    return {
        "message": outcome["message"],
        "form_fill_status": updated.form_fill_status,
        "status": updated.status,
        "form_fill_result": _parse_result(updated.form_fill_result),
    }


@router.post("/sessions/{session_id}/complete", response_model=CompleteSessionResponse)
def complete_session(session_id: int, db: Session = Depends(get_db)):
    service = build_service(db)
    schedule_service = build_schedule_service(db)
    draft_service = FormDraftService(service)

    try:
        session = service.get_session(session_id)
        if not session:
            raise ServiceError("Session not found.", status_code=404)
        if session.form_fill_status != FORM_FILL_AWAITING_APPROVAL:
            raise ServiceError(
                "Review the checkout in Discord and approve it before filling the live Microsoft Form.",
                status_code=409,
            )

        summary = service.summarize_session(session_id)
        draft = draft_service.build_draft(session_id)
        service.mark_form_fill_pending(session_id)
        filler = MicrosoftFormFiller(settings.microsoft_form_url)
        outcome = filler.fill_draft(draft)
        if outcome["status"] == FORM_FILL_PARTIAL_FAILURE:
            updated = service.mark_form_fill_partial_failure(
                session_id,
                outcome,
                "Form fill reached the review screen, but some sections need manual attention.",
            )
            return CompleteSessionResponse(
                session_id=updated.id,
                status=updated.status,
                form_fill_status=updated.form_fill_status,
                form_fill_error=updated.form_fill_error,
                draft_saved=updated.draft_saved,
                total_estimated_cost=summary.total_estimated_cost,
                item_count=summary.item_count,
                message=outcome["message"],
                form_fill_result=_parse_result(updated.form_fill_result),
            )

        updated = service.mark_form_fill_success(session_id, outcome, mark_completed=True)
        schedule_service.mark_linked_schedule_completed(session_id)
        return CompleteSessionResponse(
            session_id=updated.id,
            status=updated.status,
            form_fill_status=updated.form_fill_status,
            form_fill_error=updated.form_fill_error,
            draft_saved=updated.draft_saved,
            total_estimated_cost=summary.total_estimated_cost,
            item_count=summary.item_count,
            message=outcome["message"],
            form_fill_result=_parse_result(updated.form_fill_result),
        )
    except ServiceError as exc:
        _raise_http_error(exc)
    except Exception as exc:
        error_message = f"Automatic form fill failed. {exc}"
        updated = service.mark_form_fill_failure(
            session_id,
            error_message,
            result={"error": str(exc)},
        )
        summary = service.summarize_session(session_id)
        return CompleteSessionResponse(
            session_id=updated.id,
            status=updated.status,
            form_fill_status=updated.form_fill_status,
            form_fill_error=updated.form_fill_error,
            draft_saved=updated.draft_saved,
            total_estimated_cost=summary.total_estimated_cost,
            item_count=summary.item_count,
            message=error_message,
            form_fill_result=_parse_result(updated.form_fill_result),
        )
