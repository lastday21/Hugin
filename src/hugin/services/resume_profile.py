from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import ClassVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hugin.adapters.resume_documents import ResumeDocumentReader
from hugin.database.models import (
    AnswerTemplateModel,
    CandidateProfileModel,
    HhAccountModel,
    ProfileQuestionModel,
    ResumeModel,
    VerifiedFactModel,
)
from hugin.domain.content import ConfirmationState, ProfileQuestionState
from hugin.domain.resumes import (
    ParsedResumeProfile,
    ProfileFactReview,
    ProfileQuestionCandidate,
    ResumeDocument,
    ResumeFactCandidate,
    ResumeImportResult,
)
from hugin.services.autonomy import AutonomyPolicyService


@dataclass(frozen=True, slots=True)
class _CommonQuestion:
    key: str
    question: str
    answer_pattern: re.Pattern[str]


COMMON_QUESTIONS = (
    _CommonQuestion(
        "salary_expectation",
        "Какая минимальная и желаемая зарплата после вычета налогов?",
        re.compile(r"(?:\d[\d\s\u00a0]{3,}|\d{2,3}\s*тыс)\s*(?:руб|₽)", re.IGNORECASE),
    ),
    _CommonQuestion(
        "available_from",
        "Когда вы сможете выйти на новую работу?",
        re.compile(r"(?:готов|могу)\s+(?:выйти|приступить)|дата выхода", re.IGNORECASE),
    ),
    _CommonQuestion(
        "work_schedule",
        "Какой график работы для вас допустим?",
        re.compile(r"график работы\s*:", re.IGNORECASE),
    ),
    _CommonQuestion(
        "employment",
        "Какие виды занятости вам подходят: полная, частичная или проектная?",
        re.compile(r"тип занятости\s*:", re.IGNORECASE),
    ),
    _CommonQuestion(
        "relocation",
        "Готовы ли вы к переезду?",
        re.compile(r"(?:не\s+)?готов\w*\s+к\s+переезду", re.IGNORECASE),
    ),
    _CommonQuestion(
        "business_trips",
        "Готовы ли вы к командировкам и как часто?",
        re.compile(r"командиров", re.IGNORECASE),
    ),
    _CommonQuestion(
        "work_format",
        "Какие форматы работы вам подходят: удалённо, офис или гибрид?",
        re.compile(r"формат работы\s*:", re.IGNORECASE),
    ),
    _CommonQuestion(
        "english_level",
        "Какой у вас уровень английского языка?",
        re.compile(r"английский\s*[—-]", re.IGNORECASE),
    ),
    _CommonQuestion(
        "citizenship",
        "Какое у вас гражданство?",
        re.compile(r"гражданство\s*:", re.IGNORECASE),
    ),
    _CommonQuestion(
        "work_authorization",
        "В каких странах у вас есть разрешение на работу?",
        re.compile(r"разрешение на работу\s*:", re.IGNORECASE),
    ),
    _CommonQuestion(
        "portfolio",
        "Укажите ссылку на GitHub или портфолио.",
        re.compile(r"(?:github\.com|портфолио\s*:)", re.IGNORECASE),
    ),
    _CommonQuestion(
        "job_search_reason",
        "Почему вы сейчас ищете новую работу?",
        re.compile(r"причин\w* поиска работы\s*:", re.IGNORECASE),
    ),
    _CommonQuestion(
        "test_assignment",
        "Готовы ли вы выполнить небольшое проверочное задание?",
        re.compile(r"готов\w*\s+.*(?:тестов|проверочн)\w*\s+задан", re.IGNORECASE),
    ),
)


class ResumeProfileExtractor:
    _section_starts: ClassVar[dict[str, str]] = {
        "work_experience": "Опыт работы",
        "education": "Образование",
        "courses": "Повышение квалификации, курсы",
        "skills": "Навыки",
        "driving": "Опыт вождения",
        "about": "Дополнительная информация",
    }
    _experience_duration_summary: ClassVar[re.Pattern[str]] = re.compile(
        r"^(?:[—–-]\s*)?\d+\s+(?:год|года|лет)"
        r"(?:\s+\d+\s+(?:месяц|месяца|месяцев))?$",
        re.IGNORECASE,
    )
    _about_marker: ClassVar[re.Pattern[str]] = re.compile(
        r"(?<!\w)обо\s+мне(?:\s*[:—–-]\s*|\s+|$)",
        re.IGNORECASE,
    )

    def extract(self, document: ResumeDocument) -> ParsedResumeProfile:
        lines = self._content_lines(document.text)
        facts: list[ResumeFactCandidate] = []

        display_name = lines[0] if lines and self._looks_like_name(lines[0]) else None
        if display_name:
            facts.append(self._fact("full_name", display_name, "header"))

        title = (
            self._line_after(lines, "Желаемая должность и зарплата") or document.source_path.stem
        )
        facts.append(self._fact("desired_position", title, "desired_position"))

        for category, prefix in (
            ("location", "Проживает:"),
            ("citizenship", "Гражданство:"),
            ("employment", "Тип занятости:"),
            ("work_format", "Формат работы:"),
        ):
            value = self._line_with_prefix(lines, prefix)
            if value:
                facts.append(self._fact(category, value, category))

        relocation = next((line for line in lines if "переезд" in line.casefold()), None)
        if relocation:
            facts.append(self._fact("mobility", relocation, "mobility"))

        email = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", document.text)
        if email:
            facts.append(self._fact("email", email.group(0), "contacts"))
        phone = re.search(r"(?:\+7|8)[\s()\-\d]{9,}", document.text)
        if phone:
            facts.append(self._fact("phone", phone.group(0).strip(), "contacts"))
        telegram = re.search(r"telegram\s*:\s*(@[A-Za-z0-9_]+)", document.text, re.IGNORECASE)
        if telegram:
            facts.append(self._fact("telegram", telegram.group(1), "contacts"))
        github = re.search(
            r"(?:https?://)?github\.com/[A-Za-z0-9_.-]+", document.text, re.IGNORECASE
        )
        if github:
            facts.append(self._fact("github", github.group(0), "contacts"))

        for category in ("work_experience", "education", "courses", "skills", "about"):
            section = self._section(lines, category)
            if section:
                facts.append(self._fact(category, section, category))

        languages = self._languages(lines)
        if languages:
            facts.append(self._fact("languages", languages, "languages"))

        facts = list(dict.fromkeys(facts))
        missing = tuple(
            ProfileQuestionCandidate(question.key, question.question)
            for question in COMMON_QUESTIONS
            if question.answer_pattern.search(document.text) is None
        )
        return ParsedResumeProfile(display_name, title, tuple(facts), missing)

    @staticmethod
    def _content_lines(text: str) -> list[str]:
        lines = []
        for line in text.splitlines():
            if "Резюме обновлено" in line:
                continue
            lines.append(line)
        return lines

    @staticmethod
    def _looks_like_name(value: str) -> bool:
        return re.fullmatch(r"[А-ЯЁ][а-яё-]+(?:\s+[А-ЯЁ][а-яё-]+){1,3}", value) is not None

    @staticmethod
    def _line_after(lines: list[str], heading: str) -> str | None:
        try:
            index = lines.index(heading)
        except ValueError:
            return None
        return lines[index + 1] if index + 1 < len(lines) else None

    @staticmethod
    def _line_with_prefix(lines: list[str], prefix: str) -> str | None:
        return next(
            (line[len(prefix) :].strip() for line in lines if line.startswith(prefix)), None
        )

    def _section(self, lines: list[str], category: str) -> str | None:
        heading = self._section_starts[category]
        starts = [index for index, line in enumerate(lines) if line.startswith(heading)]
        if not starts:
            return None
        start = starts[-1]
        following = [
            index
            for index, line in enumerate(lines[start + 1 :], start + 1)
            if any(line.startswith(value) for value in self._section_starts.values())
        ]
        end = following[0] if following else len(lines)
        first_line = lines[start].removeprefix(heading).strip()
        values = [first_line, *lines[start + 1 : end]]
        if category == "work_experience":
            values = self._without_experience_duration_summary(values)
        elif category == "about":
            values = self._without_recommendations(values)
        content = "\n".join(values).strip()
        return content or None

    @classmethod
    def _without_experience_duration_summary(cls, lines: list[str]) -> list[str]:
        values = list(lines)
        while values and not values[0].strip():
            values.pop(0)
        if values and cls._experience_duration_summary.fullmatch(values[0].strip()):
            values.pop(0)
        return values

    @classmethod
    def _without_recommendations(cls, lines: list[str]) -> list[str]:
        for index, line in enumerate(lines):
            marker = cls._about_marker.search(line)
            if marker is None:
                continue
            return [line[marker.end() :].strip(), *lines[index + 1 :]]
        return lines

    @staticmethod
    def _languages(lines: list[str]) -> str | None:
        for index, line in enumerate(lines):
            if not line.startswith("Знание языков"):
                continue
            values = [line.removeprefix("Знание языков").strip()]
            for following in lines[index + 1 :]:
                if following.startswith("Навыки"):
                    break
                values.append(following)
            return "\n".join(value for value in values if value) or None
        return None

    @staticmethod
    def _fact(category: str, content: str, source_reference: str) -> ResumeFactCandidate:
        return ResumeFactCandidate(category, content.strip(), f"section:{source_reference}")


class ResumeFileStore:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def preserve(self, account_id: int, document: ResumeDocument) -> Path:
        target_dir = self._data_dir / "resumes" / f"account-{account_id}"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{document.sha256}{document.source_path.suffix.casefold()}"
        if target.exists():
            return target

        with NamedTemporaryFile(dir=target_dir, prefix="import-", delete=False) as temporary:
            temporary_path = Path(temporary.name)
        try:
            shutil.copy2(document.source_path, temporary_path)
            os.replace(temporary_path, target)
        finally:
            temporary_path.unlink(missing_ok=True)
        return target


class ResumeImportService:
    LOCAL_RESUME_ID = "local-it-active"

    def __init__(self, session: Session, data_dir: Path) -> None:
        self._session = session
        self._reader = ResumeDocumentReader()
        self._extractor = ResumeProfileExtractor()
        self._store = ResumeFileStore(data_dir)

    def import_file(
        self,
        account_id: int,
        source: Path,
        *,
        hh_resume_id: str | None = None,
        original_name: str | None = None,
    ) -> ResumeImportResult:
        account = self._session.get(HhAccountModel, account_id)
        if account is None:
            raise LookupError("Аккаунт hh.ru не найден; сначала выполните синхронизацию")

        document = self._reader.read(source)
        profile_data = self._extractor.extract(document)
        stored_path = self._store.preserve(account_id, document)

        resume = self._find_resume(account_id, profile_data.title, hh_resume_id)
        if resume is None:
            resume = ResumeModel(account_id=account_id, hh_id=self.LOCAL_RESUME_ID)
            self._session.add(resume)
            previous_hash = None
        else:
            previous_hash = resume.source_sha256

        resume.title = profile_data.title
        resume.source_type = document.source_type.value
        resume.source_reference = str(stored_path)
        resume.source_original_name = original_name or document.original_name
        resume.source_sha256 = document.sha256
        resume.source_size_bytes = document.size_bytes
        resume.source_page_count = document.page_count
        resume.content_text = document.text
        resume.imported_at = datetime.now(UTC)
        resume.is_active = True
        self._session.flush()

        profile = self._session.scalar(
            select(CandidateProfileModel).where(CandidateProfileModel.account_id == account_id)
        )
        if profile is None:
            profile = CandidateProfileModel(
                account_id=account_id,
                display_name=profile_data.display_name or account.label,
            )
            self._session.add(profile)
        elif profile_data.display_name:
            profile.display_name = profile_data.display_name
        profile.active_resume_id = resume.id
        self._session.flush()

        facts_pending = self._synchronize_facts(profile.id, resume.id, stored_path, profile_data)
        questions_pending = self._synchronize_questions(profile.id, profile_data.missing_questions)
        self._session.flush()

        return ResumeImportResult(
            resume_id=resume.id,
            title=resume.title,
            stored_path=stored_path,
            source_sha256=document.sha256,
            facts_pending=facts_pending,
            questions_pending=questions_pending,
            unchanged=previous_hash == document.sha256,
        )

    def _find_resume(
        self,
        account_id: int,
        title: str,
        hh_resume_id: str | None,
    ) -> ResumeModel | None:
        if hh_resume_id:
            resume = self._session.scalar(
                select(ResumeModel).where(
                    ResumeModel.account_id == account_id,
                    ResumeModel.hh_id == hh_resume_id,
                )
            )
            if resume is None:
                raise LookupError(
                    "Указанное резюме hh.ru не найдено; сначала выполните синхронизацию"
                )
            return resume

        site_resumes = list(
            self._session.scalars(
                select(ResumeModel).where(
                    ResumeModel.account_id == account_id,
                    ResumeModel.title == title,
                    ResumeModel.hh_id != self.LOCAL_RESUME_ID,
                    ResumeModel.is_active.is_(True),
                )
            )
        )
        if len(site_resumes) == 1:
            return site_resumes[0]
        if len(site_resumes) > 1:
            raise RuntimeError(
                "Найдено несколько резюме hh.ru с таким названием; укажите их идентификатор"
            )
        return self._session.scalar(
            select(ResumeModel).where(
                ResumeModel.account_id == account_id,
                ResumeModel.hh_id == self.LOCAL_RESUME_ID,
            )
        )

    def _synchronize_facts(
        self,
        profile_id: int,
        resume_id: int,
        stored_path: Path,
        profile_data: ParsedResumeProfile,
    ) -> int:
        existing = list(
            self._session.scalars(
                select(VerifiedFactModel).where(VerifiedFactModel.resume_id == resume_id)
            )
        )
        desired = {(fact.category, fact.content): fact for fact in profile_data.facts}
        existing_keys = {(fact.category, fact.content) for fact in existing}
        reuse_confirmed = AutonomyPolicyService(
            self._session
        ).get().reuse_confirmed_profile_facts
        confirmed_by_content = (
            {
                (fact.category, self._normalized_fact_content(fact.content)): fact
                for fact in self._session.scalars(
                    select(VerifiedFactModel).where(
                        VerifiedFactModel.profile_id == profile_id,
                        VerifiedFactModel.resume_id == resume_id,
                        VerifiedFactModel.state == ConfirmationState.CONFIRMED,
                    )
                )
            }
            if reuse_confirmed
            else {}
        )

        for fact in existing:
            if (
                fact.state == ConfirmationState.PENDING
                and (fact.category, fact.content) not in desired
            ):
                self._session.delete(fact)

        for key, candidate in desired.items():
            if key in existing_keys:
                continue
            inherited = confirmed_by_content.get(
                (
                    candidate.category,
                    self._normalized_fact_content(candidate.content),
                )
            )
            model = VerifiedFactModel(
                profile_id=profile_id,
                category=candidate.category,
                content=candidate.content,
                source_type="resume",
                source_reference=f"{stored_path}#{candidate.source_reference}",
                resume_id=resume_id,
                state=(
                    ConfirmationState.CONFIRMED
                    if inherited is not None
                    else ConfirmationState.PENDING
                ),
                actual_at=inherited.actual_at if inherited is not None else None,
                allow_in_letters=inherited.allow_in_letters if inherited is not None else False,
                allow_in_forms=inherited.allow_in_forms if inherited is not None else False,
                allow_in_messages=inherited.allow_in_messages if inherited is not None else False,
            )
            self._session.add(model)
            if inherited is not None:
                inherited.state = ConfirmationState.REJECTED
                inherited.allow_in_letters = False
                inherited.allow_in_forms = False
                inherited.allow_in_messages = False
        self._session.flush()
        return (
            self._session.scalar(
                select(func.count())
                .select_from(VerifiedFactModel)
                .where(
                    VerifiedFactModel.resume_id == resume_id,
                    VerifiedFactModel.state == ConfirmationState.PENDING,
                )
            )
            or 0
        )

    @staticmethod
    def _normalized_fact_content(value: str) -> str:
        return " ".join(value.casefold().replace("\u00a0", " ").split())

    def _synchronize_questions(
        self,
        profile_id: int,
        questions: tuple[ProfileQuestionCandidate, ...],
    ) -> tuple[ProfileQuestionCandidate, ...]:
        existing = {
            question.key: question
            for question in self._session.scalars(
                select(ProfileQuestionModel).where(ProfileQuestionModel.profile_id == profile_id)
            )
        }
        desired = {question.key: question for question in questions}
        for key, stored in existing.items():
            if stored.state == ProfileQuestionState.PENDING and key not in desired:
                self._session.delete(stored)
        for key, candidate in desired.items():
            question_model = existing.get(key)
            if question_model is None:
                question_model = ProfileQuestionModel(profile_id=profile_id, key=key)
                self._session.add(question_model)
            if question_model.state != ProfileQuestionState.ANSWERED:
                question_model.question_text = candidate.question
                question_model.state = ProfileQuestionState.PENDING
        return tuple(
            candidate
            for candidate in questions
            if existing.get(candidate.key) is None
            or existing[candidate.key].state != ProfileQuestionState.ANSWERED
        )


class ProfileQuestionService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list_pending(self, account_id: int) -> tuple[ProfileQuestionCandidate, ...]:
        profile = self._profile(account_id)
        models = self._session.scalars(
            select(ProfileQuestionModel)
            .where(
                ProfileQuestionModel.profile_id == profile.id,
                ProfileQuestionModel.state == ProfileQuestionState.PENDING,
            )
            .order_by(ProfileQuestionModel.id)
        )
        return tuple(ProfileQuestionCandidate(model.key, model.question_text) for model in models)

    def answer(self, account_id: int, key: str, answer: str) -> None:
        value = answer.strip().lstrip("\ufeff").strip()
        if not value:
            raise ValueError("Ответ не может быть пустым")
        if len(value) > 4000:
            raise ValueError("Ответ слишком длинный")
        if "???" in value or "\ufffd" in value or value.startswith(("ï»¿", "п»ї")):
            raise ValueError("Ответ содержит повреждённые символы; введите его заново")

        profile = self._profile(account_id)
        question = self._session.scalar(
            select(ProfileQuestionModel).where(
                ProfileQuestionModel.profile_id == profile.id,
                ProfileQuestionModel.key == key,
            )
        )
        if question is None:
            raise LookupError("Вопрос не найден")

        source_reference = f"profile-question:{key}"
        fact = self._session.scalar(
            select(VerifiedFactModel).where(
                VerifiedFactModel.profile_id == profile.id,
                VerifiedFactModel.source_reference == source_reference,
            )
        )
        if fact is None:
            fact = VerifiedFactModel(
                profile_id=profile.id,
                category=key,
                source_type="user",
                source_reference=source_reference,
            )
            self._session.add(fact)
        answered_at = datetime.now(UTC)
        fact.content = value
        fact.state = ConfirmationState.CONFIRMED
        fact.allow_in_forms = True
        fact.actual_at = answered_at
        self._session.flush()

        template = self._session.scalar(
            select(AnswerTemplateModel).where(
                AnswerTemplateModel.profile_id == profile.id,
                AnswerTemplateModel.key == key,
            )
        )
        if template is None:
            template = AnswerTemplateModel(profile_id=profile.id, key=key)
            self._session.add(template)
        template.question_pattern = question.question_text
        template.answer_text = value
        template.verified_fact_id = fact.id
        template.is_active = True

        question.answer_text = value
        question.state = ProfileQuestionState.ANSWERED
        question.answered_at = answered_at
        self._session.flush()

    def dismiss(self, account_id: int, key: str) -> None:
        profile = self._profile(account_id)
        question = self._session.scalar(
            select(ProfileQuestionModel).where(
                ProfileQuestionModel.profile_id == profile.id,
                ProfileQuestionModel.key == key,
            )
        )
        if question is None:
            raise LookupError("Вопрос не найден")
        question.state = ProfileQuestionState.DISMISSED
        question.answer_text = None
        question.answered_at = None
        self._session.flush()

    def _profile(self, account_id: int) -> CandidateProfileModel:
        profile = self._session.scalar(
            select(CandidateProfileModel).where(CandidateProfileModel.account_id == account_id)
        )
        if profile is None:
            raise LookupError("Профиль кандидата не найден; сначала импортируйте резюме")
        return profile


class ProfileFactService:
    MAX_CONTENT_LENGTH = 20_000

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_pending(self, account_id: int) -> tuple[ProfileFactReview, ...]:
        profile = self._profile(account_id)
        facts = self._session.scalars(
            select(VerifiedFactModel)
            .where(
                VerifiedFactModel.profile_id == profile.id,
                VerifiedFactModel.state == ConfirmationState.PENDING,
            )
            .order_by(VerifiedFactModel.id)
        )
        return tuple(ProfileFactReview(fact.id, fact.category, fact.content) for fact in facts)

    def confirm(
        self,
        account_id: int,
        fact_id: int,
        *,
        allow_in_letters: bool = False,
        allow_in_forms: bool = False,
        allow_in_messages: bool = False,
    ) -> None:
        fact = self._fact(account_id, fact_id)
        self._activate(
            fact,
            allow_in_letters=allow_in_letters,
            allow_in_forms=allow_in_forms,
            allow_in_messages=allow_in_messages,
        )
        self._session.flush()

    def correct(
        self,
        account_id: int,
        fact_id: int,
        content: str,
        *,
        allow_in_letters: bool = False,
        allow_in_forms: bool = False,
        allow_in_messages: bool = False,
    ) -> VerifiedFactModel:
        fact = self._fact(account_id, fact_id)
        value = self._validated_content(content)
        if value == fact.content:
            corrected = fact
        else:
            fact.state = ConfirmationState.REJECTED
            fact.allow_in_letters = False
            fact.allow_in_forms = False
            fact.allow_in_messages = False
            corrected = VerifiedFactModel(
                profile_id=fact.profile_id,
                category=fact.category,
                content=value,
                source_type="user",
                source_reference=f"profile-fact:{fact.id}",
                actual_at=fact.actual_at,
                resume_id=fact.resume_id,
                direction_id=fact.direction_id,
            )
            self._session.add(corrected)
            self._session.flush()
        self._activate(
            corrected,
            allow_in_letters=allow_in_letters,
            allow_in_forms=allow_in_forms,
            allow_in_messages=allow_in_messages,
        )
        self._session.flush()
        return corrected

    def reject(self, account_id: int, fact_id: int) -> None:
        fact = self._fact(account_id, fact_id)
        fact.state = ConfirmationState.REJECTED
        fact.allow_in_letters = False
        fact.allow_in_forms = False
        fact.allow_in_messages = False
        self._session.flush()

    def _activate(
        self,
        fact: VerifiedFactModel,
        *,
        allow_in_letters: bool,
        allow_in_forms: bool,
        allow_in_messages: bool,
    ) -> None:
        statement = select(VerifiedFactModel).where(
            VerifiedFactModel.profile_id == fact.profile_id,
            VerifiedFactModel.category == fact.category,
            VerifiedFactModel.state == ConfirmationState.CONFIRMED,
            VerifiedFactModel.id != fact.id,
        )
        if fact.direction_id is None:
            statement = statement.where(VerifiedFactModel.direction_id.is_(None))
        else:
            statement = statement.where(VerifiedFactModel.direction_id == fact.direction_id)
        for previous in self._session.scalars(statement):
            previous.state = ConfirmationState.REJECTED
            previous.allow_in_letters = False
            previous.allow_in_forms = False
            previous.allow_in_messages = False
        fact.state = ConfirmationState.CONFIRMED
        fact.allow_in_letters = allow_in_letters
        fact.allow_in_forms = allow_in_forms
        fact.allow_in_messages = allow_in_messages
        fact.actual_at = datetime.now(UTC)

    @classmethod
    def _validated_content(cls, content: str) -> str:
        value = content.strip().lstrip("\ufeff").strip()
        if not value:
            raise ValueError("Сведение не может быть пустым")
        if len(value) > cls.MAX_CONTENT_LENGTH:
            raise ValueError("Сведение слишком длинное")
        if "???" in value or "\ufffd" in value or value.startswith(("ï»¿", "п»ї")):
            raise ValueError("Сведение содержит повреждённые символы; введите его заново")
        return value

    def _fact(self, account_id: int, fact_id: int) -> VerifiedFactModel:
        profile = self._profile(account_id)
        fact = self._session.get(VerifiedFactModel, fact_id)
        if fact is None or fact.profile_id != profile.id:
            raise LookupError("Факт не найден")
        return fact

    def _profile(self, account_id: int) -> CandidateProfileModel:
        profile = self._session.scalar(
            select(CandidateProfileModel).where(CandidateProfileModel.account_id == account_id)
        )
        if profile is None:
            raise LookupError("Профиль кандидата не найден; сначала импортируйте резюме")
        return profile
