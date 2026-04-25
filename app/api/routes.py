import json

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
    )
    return CheckoutService(db=db, pricing=pricing, image_store=store, ai_service=ai_service)


def _raise_http_error(exc: ServiceError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


def _parse_result(payload: str | None) -> dict | None:
    if not payload:
        return None
    return json.loads(payload)


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


@router.get("/sessions/active", response_model=SessionResponse)
def get_active_session(channel_id: int = Query(...), db: Session = Depends(get_db)):
    service = build_service(db)
    session = service.get_channel_session(channel_id)
    if not session:
        raise HTTPException(status_code=404, detail="No active checkout in this channel.")
    return session


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
    raw_note: str = Form(...),
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
            raw_note=raw_note,
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
    service = build_service(db)
    try:
        summary = service.summarize_session(session_id)
    except ServiceError as exc:
        _raise_http_error(exc)

    category_options = [
        {"key": section["key"], "name": section["name"]}
        for section in get_damage_sections()
    ]
    payload = {
        "session": summary.model_dump(),
        "categories": category_options,
    }
    app_payload = json.dumps(payload).replace("</", "<\\/")
    return HTMLResponse(
        f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Checkout Review {session_id}</title>
  <style>
    :root {{
      --bg: #f3efe6;
      --card: #fffaf2;
      --ink: #1f2933;
      --accent: #285943;
      --border: #d9c9ad;
    }}
    body {{
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      background: linear-gradient(180deg, #f7f2e7 0%, #ebe2d2 100%);
      color: var(--ink);
    }}
    main {{
      max-width: 760px;
      margin: 0 auto;
      padding: 20px 16px 40px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 16px;
      margin-bottom: 16px;
      box-shadow: 0 10px 24px rgba(31, 41, 51, 0.08);
    }}
    h1, h2 {{
      margin: 0 0 12px;
    }}
    .meta {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px 12px;
      font-size: 15px;
    }}
    .item {{
      border-top: 1px solid var(--border);
      padding-top: 12px;
      margin-top: 12px;
    }}
    textarea, select, input[type="file"] {{
      width: 100%;
      box-sizing: border-box;
      margin-top: 8px;
      margin-bottom: 10px;
      padding: 10px;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: white;
      font: inherit;
    }}
    button {{
      border: 0;
      border-radius: 999px;
      padding: 10px 14px;
      margin-right: 8px;
      margin-bottom: 8px;
      background: var(--accent);
      color: white;
      font: inherit;
    }}
    button.secondary {{
      background: #8d5b2c;
    }}
    button.danger {{
      background: #a33a2b;
    }}
    .thumbs {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 8px 0 10px;
    }}
    .thumb {{
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 8px;
      background: white;
      font-size: 13px;
    }}
    .status {{
      font-size: 14px;
      color: var(--accent);
      margin-top: 8px;
      min-height: 20px;
    }}
  </style>
</head>
<body>
  <main>
    <div class="card">
      <h1>Checkout Review</h1>
      <div class="meta">
        <div><strong>Session:</strong> <span id="session-id"></span></div>
        <div><strong>Resident:</strong> <span id="resident-name"></span></div>
        <div><strong>Room:</strong> <span id="room-number"></span></div>
        <div><strong>Total:</strong> $<span id="total-cost"></span></div>
      </div>
      <div style="margin-top:12px;">
        <button onclick="fillForm()">Fill Microsoft Form</button>
        <button class="secondary" onclick="reloadData()">Refresh</button>
      </div>
      <div class="status" id="page-status"></div>
    </div>
    <div class="card">
      <h2>Damage Items</h2>
      <div id="items"></div>
    </div>
  </main>
  <script>
    const boot = {app_payload};
    const categoryOptions = boot.categories
      .map((category) => `<option value="${{category.key}}">${{category.name}}</option>`)
      .join("");

    function setStatus(message) {{
      document.getElementById("page-status").textContent = message || "";
    }}

    function escapeHtml(value) {{
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }}

    function render(session) {{
      document.getElementById("session-id").textContent = session.session_id;
      document.getElementById("resident-name").textContent = session.resident_name;
      document.getElementById("room-number").textContent = session.room_number;
      document.getElementById("total-cost").textContent = Number(session.total_estimated_cost).toFixed(2);
      const items = session.items || [];
      document.getElementById("items").innerHTML = items.map((item) => `
        <div class="item">
          <div><strong>ID ${'{'}item.id{'}'}:</strong> ${'{'}escapeHtml(item.category){'}'} | $${'{'}Number(item.estimated_cost).toFixed(2){'}'}</div>
          <textarea id="desc-${'{'}item.id{'}'}">${'{'}escapeHtml(item.raw_note){'}'}</textarea>
          <select id="cat-${'{'}item.id{'}'}">
            ${'{'}categoryOptions{'}'}
          </select>
          <div class="thumbs">
            ${'{'}(item.images || []).map((image) => `
              <div class="thumb">
                Image ${'{'}image.id{'}'}${'{'}image.is_primary ? " (primary)" : ""{'}'}<br>
                <button class="danger" onclick="removeImage(${'{'}item.id{'}'}, ${'{'}image.id{'}'})">Remove</button>
              </div>
            `).join("") || "No images yet."{'}'}
          </div>
          <input type="file" id="file-${'{'}item.id{'}'}" accept="image/*">
          <div>
            <button onclick="saveDescription(${'{'}item.id{'}'})">Save Description</button>
            <button class="secondary" onclick="saveCategory(${'{'}item.id{'}'})">Save Category</button>
            <button class="secondary" onclick="addImage(${'{'}item.id{'}'})">Add Image</button>
            <button class="danger" onclick="deleteItem(${'{'}item.id{'}'})">Delete Item</button>
          </div>
        </div>
      `).join("");
      items.forEach((item) => {{
        const select = document.getElementById(`cat-${{item.id}}`);
        if (select) select.value = item.form_section;
      }});
    }}

    async function reloadData() {{
      const response = await fetch(`/api/sessions/{session_id}/summary`);
      const data = await response.json();
      render(data);
      setStatus("Review refreshed.");
    }}

    async function saveDescription(itemId) {{
      const raw_note = document.getElementById(`desc-${{itemId}}`).value;
      const response = await fetch(`/api/sessions/{session_id}/damage-items/${'{'}itemId{'}'}/description`, {{
        method: "PUT",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ raw_note }})
      }});
      if (!response.ok) {{
        setStatus("Description update failed.");
        return;
      }}
      await reloadData();
    }}

    async function saveCategory(itemId) {{
      const category_key = document.getElementById(`cat-${{itemId}}`).value;
      const response = await fetch(`/api/sessions/{session_id}/damage-items/${'{'}itemId{'}'}/category`, {{
        method: "PUT",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ category_key }})
      }});
      if (!response.ok) {{
        setStatus("Category update failed.");
        return;
      }}
      await reloadData();
    }}

    async function deleteItem(itemId) {{
      const response = await fetch(`/api/sessions/{session_id}/damage-items/${'{'}itemId{'}'}`, {{
        method: "DELETE"
      }});
      if (!response.ok) {{
        setStatus("Delete failed.");
        return;
      }}
      await reloadData();
    }}

    async function addImage(itemId) {{
      const fileInput = document.getElementById(`file-${{itemId}}`);
      if (!fileInput.files.length) {{
        setStatus("Choose an image first.");
        return;
      }}
      const formData = new FormData();
      formData.append("image", fileInput.files[0]);
      const response = await fetch(`/api/sessions/{session_id}/damage-items/${'{'}itemId{'}'}/images`, {{
        method: "POST",
        body: formData
      }});
      if (!response.ok) {{
        setStatus("Image upload failed.");
        return;
      }}
      await reloadData();
    }}

    async function removeImage(itemId, imageId) {{
      const response = await fetch(`/api/sessions/{session_id}/damage-items/${'{'}itemId{'}'}/images/${'{'}imageId{'}'}`, {{
        method: "DELETE"
      }});
      if (!response.ok) {{
        setStatus("Image removal failed.");
        return;
      }}
      await reloadData();
    }}

    async function fillForm() {{
      setStatus("Preparing live form fill...");
      const reviewResponse = await fetch(`/api/sessions/{session_id}/review`, {{ method: "POST" }});
      if (!reviewResponse.ok) {{
        setStatus("Review check failed. Resolve any pending item first.");
        return;
      }}
      const response = await fetch(`/api/sessions/{session_id}/complete`, {{ method: "POST" }});
      const data = await response.json();
      setStatus(data.message || "Form fill finished.");
    }}

    render(boot.session);
  </script>
</body>
</html>
        """
    )


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
    try:
        service.cancel_session(session_id)
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
    except Exception:
        updated = service.mark_form_fill_failure(
            session_id,
            "Automatic form fill failed. Progress was saved for retry.",
        )
        return {
            "message": "Automatic form fill failed. Progress was saved for retry.",
            "form_fill_status": updated.form_fill_status,
            "status": updated.status,
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
    except Exception:
        updated = service.mark_form_fill_failure(
            session_id, "Automatic form fill failed. Progress was saved for retry."
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
            message="Automatic form fill failed. Progress was saved for retry.",
            form_fill_result=_parse_result(updated.form_fill_result),
        )
