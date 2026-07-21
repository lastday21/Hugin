from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from textwrap import dedent
from typing import Any

QUESTION_PROMPT_VERSION = "resume_questions_v2"
REWRITE_PROMPT_VERSION = "resume_rewrite_v1"

SYSTEM_PROMPT = """Ты редактор резюме российских ИТ-специалистов. Пиши по-русски,
конкретно и без рекламных клише. Используй только переданные факты. Не добавляй технологии,
цифры, результаты, масштаб, стадию проекта или уровень ответственности, которых нет во входных
данных. Если точных показателей нет, не выдумывай их."""


class ResumeBlockKind(StrEnum):
    WORK_EXPERIENCE = "WORK_EXPERIENCE"
    PROJECT = "PROJECT"


class ResumeQuestionTopic(StrEnum):
    PERSONAL_CONTRIBUTION = "personal_contribution"
    RESULT = "result"
    PROJECT_STATUS = "project_status"
    SCALE = "scale"
    COLLABORATION = "collaboration"


@dataclass(frozen=True, slots=True)
class ResumePromptContext:
    kind: ResumeBlockKind
    source_block: str
    target_role: str
    vacancy_context: str = ""


@dataclass(frozen=True, slots=True)
class ResumeQuestionAnswer:
    question: str
    answer: str


@dataclass(frozen=True, slots=True)
class ResumeQuestionAssessment:
    topic: ResumeQuestionTopic
    known: bool
    evidence: str | None
    question: str | None


def build_questions_prompt(context: ResumePromptContext) -> str:
    source_block, target_role, _ = _context_values(context)
    return dedent(
        f"""
        Определи, каких сведений не хватает в одном блоке опыта или проекта. Проверь ровно пять
        тем в заданном порядке:
        1. personal_contribution — личный вклад и границы ответственности;
        2. result — подтвержденный практический результат;
        3. project_status — стадия решения во время участия кандидата;
        4. scale — нагрузка, объем данных или количество пользователей;
        5. collaboration — взаимодействие с командой или смежными системами.

        Для каждой темы верни объект со свойствами:
        - topic: ключ темы из списка;
        - known: true, если ответ уже есть в исходном блоке, иначе false;
        - evidence: короткая точная выдержка из исходного блока, если known=true, иначе null;
        - question: один короткий вопрос на «вы», если known=false, иначе null.

        Правила проверки:
        - качественный результат считается ответом, даже если нет цифр;
        - если результат содержит число или точность, не требуй другую метрику;
        - передача проекта в дальнейшую разработку считается указанной стадией участия кандидата;
        - активные формулировки «разработал», «реализовал», «сам сформулировал» подтверждают
          личный вклад;
        - не спрашивай о событиях после ухода кандидата, будущих планах, сложностях и задачах,
          которых нет в исходном блоке;
        - требования направления поиска не являются фактами прошлой работы.

        Направление поиска, только как ориентир:
        {target_role}

        Тип блока: {context.kind.value}

        Исходный блок:
        {source_block}

        Верни только JSON-массив из пяти объектов в указанном порядке. Не используй разметку
        Markdown.
        """
    ).strip()


def parse_question_assessments(response: str) -> tuple[ResumeQuestionAssessment, ...]:
    payload = _json_payload(response)
    if not isinstance(payload, list) or len(payload) != len(ResumeQuestionTopic):
        raise ValueError("Модель должна вернуть проверку пяти тем")

    assessments: list[ResumeQuestionAssessment] = []
    for expected_topic, raw in zip(ResumeQuestionTopic, payload, strict=True):
        if not isinstance(raw, dict) or raw.get("topic") != expected_topic.value:
            raise ValueError("Темы проверки резюме должны идти в заданном порядке")
        known = raw.get("known")
        evidence = raw.get("evidence")
        question = raw.get("question")
        if not isinstance(known, bool):
            raise ValueError("Признак наличия ответа должен быть логическим")
        if known:
            if not isinstance(evidence, str) or not evidence.strip() or question is not None:
                raise ValueError("Для известной темы нужна выдержка без нового вопроса")
            assessment = ResumeQuestionAssessment(
                topic=expected_topic,
                known=True,
                evidence=evidence.strip(),
                question=None,
            )
        else:
            if evidence is not None or not isinstance(question, str) or not question.endswith("?"):
                raise ValueError("Для пробела нужен один короткий вопрос")
            assessment = ResumeQuestionAssessment(
                topic=expected_topic,
                known=False,
                evidence=None,
                question=question.strip(),
            )
        assessments.append(assessment)
    return tuple(assessments)


def select_missing_questions(
    assessments: tuple[ResumeQuestionAssessment, ...],
    *,
    limit: int = 3,
) -> tuple[str, ...]:
    if not 1 <= limit <= 3:
        raise ValueError("Можно запросить от одного до трех уточнений")
    return tuple(
        assessment.question
        for assessment in assessments
        if not assessment.known and assessment.question is not None
    )[:limit]


def build_rewrite_prompt(
    context: ResumePromptContext,
    answers: tuple[ResumeQuestionAnswer, ...] = (),
) -> str:
    source_block, target_role, vacancy_context = _context_values(context)
    rendered_answers = "\n\n".join(
        f"{index}. {answer.question.strip()}\n{answer.answer.strip()}"
        for index, answer in enumerate(answers, start=1)
        if answer.question.strip() and answer.answer.strip()
    )
    if answers and not rendered_answers:
        raise ValueError("Ответы кандидата не должны быть пустыми")
    if not rendered_answers:
        rendered_answers = "Уточняющих вопросов не потребовалось."

    if context.kind is ResumeBlockKind.PROJECT:
        output_format = """Название проекта без изменения смысла
        Назначение: одно короткое предложение
        Вклад:
        - от 2 до 6 пунктов
        Результат:
        - только подтвержденные результаты; раздел можно не добавлять, если результата нет
        Технологии: одна строка, только если они указаны в источниках"""
    else:
        output_format = """Задачи и вклад:
        - от 3 до 7 пунктов
        Результаты:
        - только подтвержденные результаты; раздел можно не добавлять, если результата нет
        Технологии: одна строка, только если они указаны в источниках"""

    return dedent(
        f"""
        Подготовь готовый содержательный блок места работы или проекта для ИТ-резюме на hh.ru.
        Компания, должность, даты, контакты, город и условия работы находятся вне этого блока и
        не должны появляться в ответе.

        Источники фактов:
        1. Исходный блок резюме.
        2. Ответы кандидата на уточняющие вопросы.

        Направление поиска, только как ориентир:
        {target_role}

        Повторяющиеся требования вакансий:
        {vacancy_context}

        Тип блока: {context.kind.value}

        Исходный блок:
        {source_block}

        Ответы кандидата:
        {rendered_answers}

        Собери текст по правилам:
        - используй только явно указанные факты; сведения «не указано», «неизвестно» и
          «не фиксировалось» не превращай в достижения;
        - не добавляй технологии, цифры, масштабы, руководство, промышленную эксплуатацию и
          командную работу, если они не подтверждены;
        - не подменяй содержание прошлой работы требованиями направления поиска;
        - сохрани технически важные детали, но каждый факт упомяни один раз;
        - сначала опиши назначение решения и личный вклад, затем основные задачи, затем
          результаты;
        - обязательно включи все подтвержденные результаты из исходного блока и ответов;
        - если точных измерений нет, не создавай числа и не употребляй слова «значительно» и
          «существенно»;
        - если в исходном блоке есть отдельные достижения, перенеси каждое подтвержденное
          достижение, кроме повторов и планов на будущее;
        - планы и возможные расширения не описывай как готовую реализацию;
        - начинай каждый пункт с действия кандидата: «Разработал», «Реализовал», «Настроил» и
          подобных; не начинай пункты с отглагольных существительных или пассивных конструкций;
        - результат формулируй как действие и его практический эффект, а не как общую оценку;
        - используй короткие пункты без местоимения «я», общих качеств и рекламы;
        - перед ответом проверь орфографию, падежи и согласование слов.

        Формат ответа:
        {output_format}

        Верни только готовый блок без пояснений.
        """
    ).strip()


def _context_values(context: ResumePromptContext) -> tuple[str, str, str]:
    source_block = context.source_block.strip()
    target_role = context.target_role.strip()
    vacancy_context = context.vacancy_context.strip() or "Дополнительных ориентиров нет."
    if not source_block:
        raise ValueError("Исходный блок резюме не должен быть пустым")
    if not target_role:
        raise ValueError("Нужно указать направление поиска")
    return source_block, target_role, vacancy_context


def _json_payload(response: str) -> Any:
    value = response.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, flags=re.DOTALL | re.IGNORECASE)
    if fenced is not None:
        value = fenced.group(1)
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("Модель вернула некорректный JSON") from error
