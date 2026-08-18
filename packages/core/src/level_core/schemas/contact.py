"""Contacts per person (teacher, doctor, other)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, EmailStr, Field


class ContactKind(StrEnum):
    TEACHER = "teacher"
    DOCTOR = "doctor"
    COACH = "coach"
    OTHER = "other"


class Contact(BaseModel):
    contact_id: str
    person_id: str
    kind: ContactKind
    name: str
    email: EmailStr | None = None
    phone: str | None = None
    notes: str = ""
    version: int = 1
    updated_at: datetime = Field(default_factory=datetime.utcnow)
