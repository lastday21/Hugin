from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from hugin.domain.time import as_utc
from hugin.domain.vacancies import VacancyData


class PageCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=0, ge=0)
    anchor_id: str | None = None
    anchor_at: datetime | None = None
    needs_overlap: bool = False
    seeking: bool = False
    seek_from_start: bool = False
    complete: bool = False
    last_page: int | None = None
    last_ids: tuple[str, ...] = ()

    def begin_overlap(self) -> None:
        if self.needs_overlap and not self.seeking:
            self.page = max(0, self.page - 1)
            self.seeking = True
            self.needs_overlap = False
            self.seek_from_start = self.page == 0

    def repeated_page(self, vacancies: tuple[VacancyData, ...]) -> bool:
        return bool(
            vacancies
            and self.last_page is not None
            and self.last_page != self.page
            and self.last_ids == tuple(v.hh_id for v in vacancies)
        )

    def observe(
        self, vacancies: tuple[VacancyData, ...], boundary: datetime, *, ordered: bool
    ) -> None:
        self.last_page = self.page
        self.last_ids = tuple(v.hh_id for v in vacancies)
        dated = bool(vacancies) and all(v.published_at is not None for v in vacancies)
        latest = max((as_utc(v.published_at) for v in vacancies if v.published_at), default=None)
        outside_period = ordered and dated and latest is not None and latest < boundary
        if self.seeking and not self.seek_from_start and (not vacancies or outside_period):
            self.page = 0
            self.seek_from_start = True
            return
        if not vacancies or outside_period:
            self.complete = True
            return
        if self.seeking:
            reached = self.anchor_id in self.last_ids or (
                ordered
                and dated
                and self.anchor_at is not None
                and latest is not None
                and latest < as_utc(self.anchor_at)
            )
            if reached and self.anchor_id not in self.last_ids and not self.seek_from_start:
                self.page = 0
                self.seek_from_start = True
                return
            if reached:
                self.seeking = False
            self.page += 1
            return
        last = vacancies[-1]
        self.anchor_id = last.hh_id
        self.anchor_at = as_utc(last.published_at) if last.published_at else None
        self.page += 1
        self.needs_overlap = True


class VariantCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    depth: PageCheckpoint = Field(default_factory=PageCheckpoint)
    fresh: PageCheckpoint = Field(default_factory=PageCheckpoint)
    head_id: str | None = None
    head_at: datetime | None = None
    new_head_id: str | None = None
    new_head_at: datetime | None = None


class SearchCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    boundary: datetime
    started_at: datetime
    completed_at: datetime | None = None
    fresh_started_at: datetime | None = None
    depth_variant_index: int = Field(default=0, ge=0)
    fresh_variant_index: int = Field(default=0, ge=0)
    variants: list[VariantCoverage]
