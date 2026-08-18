"""Contacts CRUD grouped by person + kind."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from level_core.schemas import Contact, ContactKind
from level_core.storage.base import UserStore
from level_core.storage.care_store import new_id
from pydantic import BaseModel, EmailStr

from level_api.deps import get_user_store

router = APIRouter()


class UpsertContact(BaseModel):
    contact_id: str | None = None
    person_id: str
    kind: ContactKind
    name: str
    email: EmailStr | None = None
    phone: str | None = None
    notes: str = ""


@router.get("")
async def list_contacts(store: UserStore = Depends(get_user_store)) -> dict[str, Any]:
    contacts = [c.model_dump(mode="json") for c in await store.contacts.list()]
    return {"contacts": contacts}


@router.post("")
async def upsert(body: UpsertContact, store: UserStore = Depends(get_user_store)) -> dict[str, Any]:
    contact = Contact(
        contact_id=body.contact_id or new_id("con"),
        person_id=body.person_id,
        kind=body.kind,
        name=body.name.strip(),
        email=body.email,
        phone=body.phone,
        notes=body.notes,
    )
    written = await store.contacts.upsert(contact)
    return written.model_dump(mode="json")


@router.delete("/{contact_id}")
async def delete(contact_id: str, store: UserStore = Depends(get_user_store)) -> dict[str, str]:
    existing = await store.contacts.get(contact_id)
    if not existing:
        raise HTTPException(status_code=404, detail="not_found")
    await store.contacts.delete(contact_id)
    return {"status": "deleted"}
