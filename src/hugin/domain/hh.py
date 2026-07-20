from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HhResumeData:
    hh_id: str
    title: str

    def __post_init__(self) -> None:
        if not self.hh_id or len(self.hh_id) > 64:
            raise ValueError("Некорректный идентификатор резюме hh.ru")
        if not self.title or len(self.title) > 255:
            raise ValueError("Некорректное название резюме hh.ru")


@dataclass(frozen=True, slots=True)
class HhProfileData:
    external_id: str
    label: str
    resumes: tuple[HhResumeData, ...]

    def __post_init__(self) -> None:
        if not self.external_id or len(self.external_id) > 128:
            raise ValueError("Некорректный идентификатор аккаунта hh.ru")
        if not self.label or len(self.label) > 255:
            raise ValueError("Некорректное имя аккаунта hh.ru")
        resume_ids = [resume.hh_id for resume in self.resumes]
        if len(resume_ids) != len(set(resume_ids)):
            raise ValueError("hh.ru вернул повторяющиеся резюме")
