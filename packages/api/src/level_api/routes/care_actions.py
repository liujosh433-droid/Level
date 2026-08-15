"""Care actions — missing usuals, school paper, sick-day send.

Writes stay behind Hold/Run. No coverage search, no friend texts.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from level_api.auth_deps import require_user
from level_api.dependencies import (
    get_calendar_sync_store,
    get_memory,
    get_proposal_store,
    get_token_store,
)
from level_core.auth.tokens import TokenStore
from level_core.calendar.agenda_sync import inject_event_into_agenda_cache, refresh_agenda_cache
from level_core.calendar.proposals import ProposalStore
from level_core.calendar.school import (
    SchoolPaperExtract,
    attach_school_email,
    build_school_send_proposal,
    draft_contact_note,
    draft_paper_hold_title,
    events_for_person_on_date,
    match_contacts_by_role,
    match_people_by_names,
    person_contacts,
    school_send_target,
)
from level_core.calendar.sync_state import CalendarSyncStore
from level_core.calendar.usuals import (
    DEFAULT_TZ,
    apply_usual_resolution,
    usual_window_datetimes,
)
from level_core.config import get_settings
from level_core.errors import ModelUnavailable
from level_core.ingest.google_live import create_calendar_event
from level_core.memory.base import MemoryBank
from level_core.models.base import GenerationRequest, PromptMedia
from level_core.models.factory import build_gemini_client
from level_core.observability.logger import get_logger
from level_core.profile.people_usuals import hydrate_people_from_roles
from level_core.profile.synthesize import invalidate_care_graph_cache
from level_core.schemas.base import _new_id
from level_core.schemas.care import (
    CareContact,
    CarePerson,
    CareProfile,
    CareRoleId,
    ensure_self_care_person,
    held_care_people,
    seed_contacts,
)
from level_core.schemas.commitment import CommitmentProposal
from level_core.schemas.profile import BulletStatus

_logger = get_logger(__name__)

router = APIRouter(prefix="/v1/care", tags=["care-actions"])


class UsualResolveRequest(BaseModel):
    usual_id: str = Field(min_length=1, max_length=64)
    action: str = Field(min_length=2, max_length=40)
    on_date: str | None = Field(default=None, description="YYYY-MM-DD")


class UsualResolveResponse(BaseModel):
    ok: bool = True
    google_event_id: str | None = None


class ContactIn(BaseModel):
    contact_id: str = ""
    role: str = Field(min_length=1, max_length=40)
    name: str = Field(default="", max_length=80)
    email: str = Field(default="", max_length=200)


class PersonContactsRequest(BaseModel):
    person_id: str = Field(min_length=1, max_length=64)
    contacts: list[ContactIn] = Field(default_factory=list)


class AddPersonRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)
    their_relation: str = Field(default="", max_length=48)
    care_role_id: str = Field(default="child_care", max_length=40)


class EnsureSelfRequest(BaseModel):
    display_name: str = Field(default="", max_length=80)


_ADDABLE_ROLES = {CareRoleId.CHILD_CARE.value, CareRoleId.ELDER_CARE.value}


class SchoolContactResponse(BaseModel):
    ok: bool = True
    person_id: str | None = None


class SchoolPaperResponse(BaseModel):
    proposal: CommitmentProposal | None = None
    ask: str | None = None


_SCHOOL_FILE_MAX = 8 * 1024 * 1024
_SCHOOL_MIME = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
    "text/plain",
}


def parse_on_date(raw: str | None, *, fallback: date | None = None) -> date:
    if raw:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            pass
    return fallback or datetime.now(tz=DEFAULT_TZ).date()


def _find_usual(
    care: CareProfile, usual_id: str
) -> tuple[CarePerson, object] | None:
    for person in care.people_profiles:
        for usual in person.usuals:
            if usual.usual_id == usual_id:
                return person, usual
    return None


@router.post("/usuals/resolve", response_model=UsualResolveResponse)
async def resolve_usual(
    payload: UsualResolveRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
    tokens: TokenStore = Depends(get_token_store),
    sync_store: CalendarSyncStore = Depends(get_calendar_sync_store),
) -> UsualResolveResponse:
    care = await memory.manifestos.get_care_profile(user_id=user_id)
    if care is None:
        raise HTTPException(status_code=404, detail="Care Profile not found.")
    pair = _find_usual(care, payload.usual_id)
    if pair is None:
        raise HTTPException(status_code=404, detail="Usual not found.")
    person, usual = pair
    action = payload.action.strip().lower().replace(" ", "_").replace("-", "_")
    on_date = parse_on_date(payload.on_date)
    event_id: str | None = None

    if action in {"put_back", "put_it_back"}:
        token = await tokens.get_google_token(user_id)
        if token is None:
            raise HTTPException(
                status_code=400, detail="Connect Google Calendar on Sources first."
            )
        start, end = usual_window_datetimes(usual, on_date)
        try:
            created = await create_calendar_event(
                token,
                summary=usual.label,
                start=start,
                end=end,
                timezone_name="America/Los_Angeles",
                description=f"Put back from usual for {person.display_name}.",
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502, detail=f"Calendar write failed: {exc}"
            ) from exc
        event_id = created.get("id")
        tz_name = "America/Los_Angeles"
        wall = ZoneInfo(tz_name)
        try:
            await inject_event_into_agenda_cache(
                user_id=user_id,
                sync_store=sync_store,
                google_event={
                    "id": event_id or f"level-usual:{usual.usual_id}:{on_date.isoformat()}",
                    "summary": usual.label,
                    "status": "confirmed",
                    "start": {
                        "dateTime": start.astimezone(wall).isoformat(timespec="seconds"),
                        "timeZone": tz_name,
                    },
                    "end": {
                        "dateTime": end.astimezone(wall).isoformat(timespec="seconds"),
                        "timeZone": tz_name,
                    },
                },
            )
        except Exception:  # noqa: BLE001
            _logger.warning("usual_put_back_cache_failed", user_id=user_id)
        background_tasks.add_task(
            refresh_agenda_cache, user_id=user_id, token=token, sync_store=sync_store
        )

    care = apply_usual_resolution(
        care, usual_id=payload.usual_id, action=action, on_date=on_date
    )
    invalidate_care_graph_cache(user_id)
    await memory.manifestos.save_care_profile(care)
    return UsualResolveResponse(ok=True, google_event_id=event_id)


async def extract_school_paper(
    text: str,
    *,
    media: tuple[PromptMedia, ...] = (),
) -> SchoolPaperExtract:
    settings = get_settings()
    gemini = build_gemini_client(settings)
    prompt = (
        f"Form text:\n{text[:8000]}"
        if text.strip()
        else "Extract the school form from the attached file."
    )
    resp = await gemini.generate(
        GenerationRequest(
            model_id=settings.fast_model,
            system_instruction=(
                "Extract a school form the caregiver uploaded or pasted. "
                "deadline as YYYY-MM-DD when present. "
                "to_email is the school/teacher address on the form — never a friend. "
                "person_name is the student named on the form, or empty. "
                "subject and body are a short reply they could send. JSON only."
            ),
            prompt=prompt,
            response_schema=SchoolPaperExtract.model_json_schema(),
            temperature=0.1,
            max_output_tokens=400,
            media=media,
        )
    )
    return SchoolPaperExtract.model_validate(json.loads(resp.text))


async def _read_school_upload(upload: UploadFile) -> PromptMedia:
    mime = (upload.content_type or "").split(";")[0].strip().lower()
    name = (upload.filename or "form").lower()
    if mime not in _SCHOOL_MIME:
        if name.endswith(".pdf"):
            mime = "application/pdf"
        elif name.endswith((".jpg", ".jpeg")):
            mime = "image/jpeg"
        elif name.endswith(".png"):
            mime = "image/png"
        elif name.endswith(".webp"):
            mime = "image/webp"
        elif name.endswith(".txt"):
            mime = "text/plain"
        else:
            raise HTTPException(
                status_code=400,
                detail="Upload a PDF, photo, or text file of the form.",
            )
    raw = await upload.read()
    if not raw:
        raise HTTPException(status_code=400, detail="That file was empty.")
    if len(raw) > _SCHOOL_FILE_MAX:
        raise HTTPException(status_code=400, detail="File is too large (8 MB max).")
    return PromptMedia(mime_type=mime, data=raw, filename=(upload.filename or "form")[:80])


@router.post("/people", response_model=SchoolContactResponse)
async def add_care_person(
    payload: AddPersonRequest,
    user_id: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
) -> SchoolContactResponse:
    name = payload.display_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required.")
    role = (payload.care_role_id or CareRoleId.CHILD_CARE.value).strip()
    if role not in _ADDABLE_ROLES:
        raise HTTPException(
            status_code=400,
            detail="Add a child or someone in elder care.",
        )
    care = await memory.manifestos.get_care_profile(user_id=user_id)
    if care is None:
        care = CareProfile(user_id=user_id)
    if any(p.display_name.lower() == name.lower() for p in care.people_profiles):
        existing = next(
            p for p in care.people_profiles if p.display_name.lower() == name.lower()
        )
        return SchoolContactResponse(ok=True, person_id=existing.person_id)
    relation = payload.their_relation.strip()[:48]
    if not relation:
        relation = "child" if role == CareRoleId.CHILD_CARE.value else "elder"
    person = CarePerson(
        display_name=name,
        their_relation=relation,
        care_role_id=role,
        status=BulletStatus.ACCEPTED,
        contacts=seed_contacts(role),
    )
    care = care.model_copy(
        update={
            "people_profiles": [*care.people_profiles, person],
            "version": int(care.version or 1) + 1,
        }
    )
    invalidate_care_graph_cache(user_id)
    await memory.manifestos.save_care_profile(care)
    return SchoolContactResponse(ok=True, person_id=person.person_id)


@router.post("/people/self", response_model=SchoolContactResponse)
async def ensure_self_person(
    payload: EnsureSelfRequest,
    user_id: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
) -> SchoolContactResponse:
    care = await memory.manifestos.get_care_profile(user_id=user_id)
    if care is None:
        care = CareProfile(user_id=user_id)
    next_care, person = ensure_self_care_person(care, payload.display_name)
    next_care = hydrate_people_from_roles(next_care)
    if next_care.version != care.version:
        invalidate_care_graph_cache(user_id)
        await memory.manifestos.save_care_profile(next_care)
    return SchoolContactResponse(ok=True, person_id=person.person_id)


@router.post("/people/contacts", response_model=SchoolContactResponse)
async def save_person_contacts(
    payload: PersonContactsRequest,
    user_id: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
) -> SchoolContactResponse:
    care = await memory.manifestos.get_care_profile(user_id=user_id)
    if care is None:
        raise HTTPException(status_code=404, detail="Care Profile not found.")
    people = []
    found = False
    for person in care.people_profiles:
        if person.person_id != payload.person_id:
            people.append(person)
            continue
        found = True
        contacts: list[CareContact] = []
        for row in payload.contacts[:12]:
            role = row.role.strip()[:40]
            if not role:
                continue
            contacts.append(
                CareContact(
                    contact_id=row.contact_id or _new_id(),
                    role=role,
                    name=row.name.strip()[:80],
                    email=row.email.strip()[:200],
                )
            )
        people.append(person.model_copy(update={"contacts": contacts}))
    if not found:
        raise HTTPException(status_code=404, detail="Person not found.")
    care = care.model_copy(
        update={
            "people_profiles": people,
            "version": int(care.version or 1) + 1,
        }
    )
    invalidate_care_graph_cache(user_id)
    await memory.manifestos.save_care_profile(care)
    return SchoolContactResponse(ok=True, person_id=payload.person_id)


@router.post("/school-paper", response_model=SchoolPaperResponse)
async def school_paper(
    user_id: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
    store: ProposalStore = Depends(get_proposal_store),
    text: str = Form(default=""),
    file: UploadFile | None = File(default=None),
) -> SchoolPaperResponse:
    pasted = (text or "").strip()
    media: tuple[PromptMedia, ...] = ()
    if file is not None and file.filename:
        media = (await _read_school_upload(file),)
    if len(pasted) < 8 and not media:
        raise HTTPException(
            status_code=400,
            detail="Upload a form or paste at least a short excerpt.",
        )
    care = await memory.manifestos.get_care_profile(user_id=user_id)
    try:
        extract = await extract_school_paper(pasted, media=media)
    except (ModelUnavailable, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Could not read the form: {exc}") from exc

    people = match_people_by_names(care, [extract.person_name] if extract.person_name else [])
    if extract.person_name and not people and care and care.people_profiles:
        names = ", ".join(p.display_name for p in held_care_people(care)[:8])
        return SchoolPaperResponse(
            ask=f"Which person is this form for? Level knows: {names}."
        )
    person = people[0] if people else None
    to_email = (extract.to_email or "").strip()
    if person and care:
        if to_email:
            care = attach_school_email(
                care, person_id=person.person_id, email=to_email
            )
            await memory.manifestos.save_care_profile(care)
        if not to_email:
            to_email, _ = school_send_target(person)
    if not to_email:
        return SchoolPaperResponse(
            ask="Add the school email from the form (or on this person) before Level can send."
        )

    hold_title = draft_paper_hold_title(extract, person)
    deadline = (extract.deadline or "").strip()[:10]
    who = person.display_name if person else "this person"
    proposal = build_school_send_proposal(
        user_id=user_id,
        user_text=(pasted or (media[0].filename if media else "School form"))[:400],
        people=people,
        to_email=to_email,
        subject=extract.subject or hold_title,
        body=extract.body or f"Please find the signed form for {who}.\n",
        cancel_event_ids=[],
        level_message=(
            f"Hold: {hold_title}"
            + (f" on {deadline}" if deadline else "")
            + f". Run sends to {to_email}."
        ),
        hold_on_calendar=bool(deadline),
        hold_date=deadline or None,
        hold_title=hold_title,
    )
    await store.save(proposal)
    return SchoolPaperResponse(proposal=proposal)


async def propose_sick_day_notes(
    *,
    user_id: str,
    user_text: str,
    care: CareProfile,
    named: list[str],
    events: list[dict[str, str | None]],
    store: ProposalStore,
    on_date: date | None = None,
    contact_role: str = "",
    cancel_today: bool = True,
    from_name: str = "",
) -> tuple[list[CommitmentProposal], str | None]:
    """Build one Hold/Run proposal per matched person. Ask if ambiguous."""
    day = on_date or datetime.now(tz=DEFAULT_TZ).date()
    people = match_people_by_names(care, named)
    known = [p.display_name for p in held_care_people(care) if p.display_name][:8]
    if not named or (not people and known):
        if len(known) == 1:
            people = match_people_by_names(care, known)
        else:
            listed = ", ".join(known) if known else "no one on Contacts yet"
            return [], f"Who should this note be about? Level knows: {listed}."
    proposals: list[CommitmentProposal] = []
    asks: list[str] = []
    role = (contact_role or "teacher").strip()
    for person in people:
        hits = match_contacts_by_role(person, role) if role else []
        if len(hits) > 1:
            labels = ", ".join(
                f"{c.role}" + (f" ({c.name})" if c.name else "") for c in hits
            )
            asks.append(
                f"{person.display_name} has more than one {role}: {labels}. Which one?"
            )
            continue
        contact = hits[0] if hits else None
        to_email = (contact.email if contact else "") or school_send_target(
            person, role=role
        )[0]
        if not to_email:
            roles = ", ".join(c.role for c in person_contacts(person)) or "none yet"
            asks.append(
                f"Add a {role} email for {person.display_name} on Contacts "
                f"(saved roles: {roles})."
            )
            continue
        subj, note = draft_contact_note(
            person=person,
            on_date=day,
            contact=contact,
            reason="will be absent today" if cancel_today else "",
            from_name=from_name,
        )
        cancel_ids = []
        if cancel_today:
            cancel_ids = [
                str(ev.get("id") or "")
                for ev in events_for_person_on_date(
                    care=care, events=events, person=person, on_date=day
                )
                if ev.get("id")
            ]
        who = (contact.name or contact.role) if contact else role
        proposal = build_school_send_proposal(
            user_id=user_id,
            user_text=user_text,
            people=[person],
            to_email=to_email,
            subject=subj,
            body=note,
            cancel_event_ids=cancel_ids,
            level_message=(
                f"Preview: email {who} ({to_email}) about {person.display_name}."
                + (
                    f" Also cancels {len(cancel_ids)} event"
                    f"{'' if len(cancel_ids) == 1 else 's'} today."
                    if cancel_ids
                    else ""
                )
            ),
        )
        await store.save(proposal)
        proposals.append(proposal)
    return proposals, " ".join(asks) or None


__all__ = [
    "parse_on_date",
    "propose_sick_day_notes",
    "router",
]
