from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

Kind = Literal["world", "character", "lore", "scenario", "rule"]
Status = Literal["draft", "published"]


class EntryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Kind
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=2000)
    content: str = Field(default="", max_length=50000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    world_id: UUID | None = None
    status: Status = "draft"

    @field_validator("title")
    @classmethod
    def clean_title(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Title cannot be blank")
        return value

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, value):
        result = list(dict.fromkeys(tag.strip() for tag in value if tag.strip()))
        if any(len(tag) > 60 for tag in result):
            raise ValueError("Each tag must be at most 60 characters")
        return result


class EntryUpdate(EntryInput):
    expected_revision: int = Field(ge=1)


class Entry(EntryInput):
    id: UUID
    revision: int = Field(ge=1)
    created_at: str
    updated_at: str


class EntrySummary(BaseModel):
    id: UUID
    kind: Kind
    title: str
    summary: str
    tags: list[str]
    world_id: UUID | None
    status: Status
    revision: int
    created_at: str
    updated_at: str


class EntryList(BaseModel):
    items: list[EntrySummary]
    total: int
    limit: int
    offset: int


class HistoryItem(BaseModel):
    entry_id: UUID
    revision: int = Field(ge=1)
    snapshot: Entry
    created_at: str


class HistoryList(BaseModel):
    items: list[HistoryItem]


class RestoreInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    expected_revision: int = Field(ge=1)


class Backup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["airp-backup-v1"]
    exported_at: str = ""
    entries: list[Entry]
    histories: list[HistoryItem] = Field(default_factory=list)


class ContextSource(BaseModel):
    id: UUID
    kind: Kind
    title: str
    world_id: UUID | None
    revision: int
    updated_at: str


class ContextResult(BaseModel):
    generated_at: str
    entries: list[ContextSource]
    context: str
    truncated: bool
