import {
  AlertTriangle,
  ArrowDown,
  BarChart3,
  CheckCircle2,
  ChevronRight,
  ClipboardList,
  History,
  Plus,
  Save,
  Target,
  X,
} from "lucide-react";
import { type FormEvent, useEffect, useMemo, useState } from "react";

import {
  assessDevelopmentDirection,
  createDevelopmentItem,
  updateDevelopmentItem,
} from "../api";
import type {
  Development,
  DevelopmentDirection,
  DevelopmentItem,
  DevelopmentItemInput,
  DevelopmentItemKind,
  DevelopmentItemStatus,
  DevelopmentPriority,
  QualityLevel,
} from "../types";
import "./development.css";

type Toast = { kind: "success" | "error"; message: string };

interface DevelopmentViewProps {
  development: Development;
  onChanged: (development: Development) => void;
  onToast: (toast: Toast) => void;
}

const confidenceNames: Record<QualityLevel, string> = {
  LOW: "Низкая",
  MEDIUM: "Средняя",
  HIGH: "Высокая",
};

const criticalityNames: Record<QualityLevel, string> = {
  LOW: "Обычная",
  MEDIUM: "Важная",
  HIGH: "Критичная",
};

const kindNames: Record<DevelopmentItemKind, string> = {
  PROBLEM: "Проблема",
  TASK: "Задача",
  HYPOTHESIS: "Гипотеза",
  MEASUREMENT: "Измерение",
  CHECK: "Проверка",
  IMPROVEMENT: "Улучшение",
};

const statusNames: Record<DevelopmentItemStatus, string> = {
  IDEA: "Идея",
  PLANNED: "Запланировано",
  IN_PROGRESS: "В работе",
  VERIFYING: "Проверяется",
  DONE: "Готово",
  REJECTED: "Отклонено",
  WAITING_EXTERNAL: "Ждёт внешней проверки",
};

const priorityNames: Record<DevelopmentPriority, string> = {
  UNASSIGNED: "Не задан",
  LOW: "Низкий",
  MEDIUM: "Средний",
  HIGH: "Высокий",
  CRITICAL: "Критический",
};

const activeStatuses = new Set<DevelopmentItemStatus>([
  "IDEA",
  "PLANNED",
  "IN_PROGRESS",
  "VERIFYING",
  "WAITING_EXTERNAL",
]);

function emptyItem(directionKey: string): DevelopmentItemInput {
  return {
    kind: "TASK",
    title: "",
    direction_key: directionKey,
    status: "IDEA",
    priority: "UNASSIGNED",
    expected_metric: "",
    evidence: "",
    verification_method: "",
    next_step: "",
    actual_result: "",
    reference_codes: [],
    author: "Пользователь",
  };
}

function scoreClass(score: number): string {
  if (score < 2.5) return "weak";
  if (score < 3.75) return "watch";
  return "strong";
}

function readableError(reason: unknown): string {
  return reason instanceof Error ? reason.message : "Не удалось сохранить изменения";
}

function formatDate(value: string): string {
  return new Date(value).toLocaleString("ru-RU", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function DevelopmentView({
  development,
  onChanged,
  onToast,
}: DevelopmentViewProps) {
  const [selectedBlockKey, setSelectedBlockKey] = useState(
    development.blocks[0]?.key ?? "",
  );
  const selectedBlock =
    development.blocks.find((block) => block.key === selectedBlockKey) ??
    development.blocks[0];
  const [selectedDirectionKey, setSelectedDirectionKey] = useState(
    selectedBlock?.directions[0]?.key ?? "",
  );
  const [itemFormOpen, setItemFormOpen] = useState(false);
  const [editingItemId, setEditingItemId] = useState<number | null>(null);
  const [assessmentOpen, setAssessmentOpen] = useState(false);
  const [showAllItems, setShowAllItems] = useState(false);
  const [saving, setSaving] = useState(false);
  const [itemDraft, setItemDraft] = useState<DevelopmentItemInput>(() =>
    emptyItem(selectedDirectionKey),
  );

  const allDirections = useMemo(
    () => development.blocks.flatMap((block) => block.directions),
    [development.blocks],
  );
  const selectedDirection =
    allDirections.find((direction) => direction.key === selectedDirectionKey) ??
    selectedBlock?.directions[0];
  const assessments = development.assessments
    .filter((assessment) => assessment.direction_key === selectedDirection?.key)
    .sort((left, right) => right.id - left.id);
  const visibleItems = development.items
    .filter((item) => showAllItems || item.direction_key === selectedDirection?.key)
    .sort((left, right) => {
      const leftDone = activeStatuses.has(left.status) ? 0 : 1;
      const rightDone = activeStatuses.has(right.status) ? 0 : 1;
      return leftDone - rightDone || right.id - left.id;
    });
  const activeItems = development.items.filter((item) => activeStatuses.has(item.status));
  const confirmedWeak = [...allDirections]
    .filter(
      (direction) =>
        direction.current_assessment.confidence !== "LOW" &&
        direction.current_assessment.score < 3.5,
    )
    .sort(
      (left, right) =>
        left.current_assessment.score - right.current_assessment.score ||
        (left.criticality === "HIGH" ? -1 : 1),
    );
  const needsMeasurement = allDirections.filter(
    (direction) => direction.current_assessment.confidence === "LOW",
  );

  useEffect(() => {
    if (!selectedBlock) return;
    if (!selectedBlock.directions.some((item) => item.key === selectedDirectionKey)) {
      setSelectedDirectionKey(selectedBlock.directions[0]?.key ?? "");
    }
  }, [selectedBlock, selectedDirectionKey]);

  useEffect(() => {
    if (!selectedDirection) return;
    setItemDraft((current) => ({ ...current, direction_key: selectedDirection.key }));
  }, [selectedDirection]);

  function chooseDirection(direction: DevelopmentDirection) {
    setSelectedBlockKey(direction.block_key);
    setSelectedDirectionKey(direction.key);
  }

  async function submitAssessment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedDirection || saving) return;
    const form = new FormData(event.currentTarget);
    setSaving(true);
    try {
      const next = await assessDevelopmentDirection(selectedDirection.key, {
        score: Number(form.get("score")),
        confidence: String(form.get("confidence")) as QualityLevel,
        evidence: String(form.get("evidence")),
        next_step: String(form.get("next_step")),
        author: "Пользователь",
      });
      onChanged(next);
      setAssessmentOpen(false);
      onToast({ kind: "success", message: "Новая оценка сохранена в истории" });
    } catch (reason) {
      onToast({ kind: "error", message: readableError(reason) });
    } finally {
      setSaving(false);
    }
  }

  async function submitItem(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving) return;
    setSaving(true);
    try {
      const next = editingItemId
        ? await updateDevelopmentItem(editingItemId, itemDraft)
        : await createDevelopmentItem(itemDraft);
      onChanged(next);
      setItemDraft(emptyItem(selectedDirection?.key ?? ""));
      setEditingItemId(null);
      setItemFormOpen(false);
      onToast({ kind: "success", message: "Рабочая запись сохранена" });
    } catch (reason) {
      onToast({ kind: "error", message: readableError(reason) });
    } finally {
      setSaving(false);
    }
  }

  async function changeItemStatus(item: DevelopmentItem, status: DevelopmentItemStatus) {
    if (saving) return;
    if (status === "DONE" && !item.actual_result.trim()) {
      setEditingItemId(item.id);
      setItemDraft({
        kind: item.kind,
        title: item.title,
        direction_key: item.direction_key,
        status,
        priority: item.priority,
        expected_metric: item.expected_metric,
        evidence: item.evidence,
        verification_method: item.verification_method,
        next_step: item.next_step,
        actual_result: item.actual_result,
        reference_codes: item.reference_codes,
        author: "Пользователь",
      });
      setItemFormOpen(true);
      onToast({
        kind: "error",
        message: "Перед завершением запишите фактический результат",
      });
      return;
    }
    setSaving(true);
    try {
      const next = await updateDevelopmentItem(item.id, {
        kind: item.kind,
        title: item.title,
        direction_key: item.direction_key,
        status,
        priority: item.priority,
        expected_metric: item.expected_metric,
        evidence: item.evidence,
        verification_method: item.verification_method,
        next_step: item.next_step,
        actual_result: item.actual_result,
        reference_codes: item.reference_codes,
        author: "Пользователь",
      });
      onChanged(next);
      onToast({ kind: "success", message: "Состояние работы обновлено" });
    } catch (reason) {
      onToast({ kind: "error", message: readableError(reason) });
    } finally {
      setSaving(false);
    }
  }

  function openNewItem() {
    setEditingItemId(null);
    setItemDraft(emptyItem(selectedDirection.key));
    setItemFormOpen(true);
  }

  function openItem(item: DevelopmentItem) {
    setEditingItemId(item.id);
    setItemDraft({
      kind: item.kind,
      title: item.title,
      direction_key: item.direction_key,
      status: item.status,
      priority: item.priority,
      expected_metric: item.expected_metric,
      evidence: item.evidence,
      verification_method: item.verification_method,
      next_step: item.next_step,
      actual_result: item.actual_result,
      reference_codes: item.reference_codes,
      author: "Пользователь",
    });
    setItemFormOpen(true);
  }

  if (!selectedBlock || !selectedDirection) {
    return <div className="development-empty">Направления развития ещё не настроены.</div>;
  }

  return (
    <section className="development-view">
      <div className="development-intro">
        <div>
          <span className="eyebrow">Карта качества продукта</span>
          <h2>Где Hugin теряет результат и что улучшать следующим</h2>
          <p>
            Оценка показывает текущее состояние, уверенность — насколько она подтверждена.
            Предварительные числа не считаются фактом, пока нет измерения.
            {" "}Балл приложения не является вероятностью трудоустройства.
          </p>
        </div>
        <div className="development-summary">
          <div><strong>{development.blocks.length}</strong><span>блоков</span></div>
          <div><strong>{allDirections.length}</strong><span>направления</span></div>
          <div><strong>{activeItems.length}</strong><span>работ в плане</span></div>
        </div>
      </div>

      <div className="development-blocks" aria-label="Блоки качества">
        {development.blocks.map((block) => (
          <button
            type="button"
            key={block.key}
            className={block.key === selectedBlock.key ? "development-block selected" : "development-block"}
            onClick={() => setSelectedBlockKey(block.key)}
          >
            <div className={`score-ring ${scoreClass(block.score)}`} style={{ "--score": `${block.score * 20}%` } as React.CSSProperties}>
              <strong>{block.score.toFixed(1)}</strong><span>из 5</span>
            </div>
            <div className="development-block-copy">
              <strong>{block.name}</strong>
              <span className={`quality-chip confidence-${block.confidence.toLowerCase()}`}>
                Уверенность: {confidenceNames[block.confidence]}
              </span>
              <small><ArrowDown size={13} /> {block.bottleneck_name}</small>
            </div>
          </button>
        ))}
      </div>

      <div className="development-focus-grid">
        <article className="focus-card confirmed">
          <div className="focus-heading"><Target size={19} /><div><strong>Подтверждённые узкие места</strong><span>Сначала устраняем причину</span></div></div>
          {confirmedWeak.length ? confirmedWeak.slice(0, 3).map((direction) => (
            <button type="button" key={direction.key} onClick={() => chooseDirection(direction)}>
              <span>{direction.name}</span><strong>{direction.current_assessment.score.toFixed(1)}</strong><ChevronRight size={16} />
            </button>
          )) : <p>Подтверждённых слабых мест пока нет.</p>}
        </article>
        <article className="focus-card measurement">
          <div className="focus-heading"><BarChart3 size={19} /><div><strong>Сначала измерить</strong><span>Число пока предварительное</span></div></div>
          {needsMeasurement.length ? needsMeasurement.slice(0, 3).map((direction) => (
            <button type="button" key={direction.key} onClick={() => chooseDirection(direction)}>
              <span>{direction.name}</span><em>нет замера</em><ChevronRight size={16} />
            </button>
          )) : <p>Все оценки уже имеют подтверждение.</p>}
        </article>
      </div>

      <div className="development-workspace-grid">
        <article className="development-panel directions-panel">
          <div className="panel-heading">
            <div><span className="eyebrow">Блок {selectedBlock.position}</span><h3>{selectedBlock.name}</h3></div>
            <span>{selectedBlock.directions.length} направлений</span>
          </div>
          <div className="direction-list">
            {selectedBlock.directions.map((direction) => (
              <button
                type="button"
                key={direction.key}
                className={direction.key === selectedDirection.key ? "direction-row selected" : "direction-row"}
                onClick={() => setSelectedDirectionKey(direction.key)}
              >
                <span className={`direction-score ${scoreClass(direction.current_assessment.score)}`}>
                  {direction.current_assessment.score.toFixed(1)}
                </span>
                <span className="direction-copy"><strong>{direction.name}</strong><small>{direction.metric}</small></span>
                <span className={`confidence-dot confidence-${direction.current_assessment.confidence.toLowerCase()}`} title={`Уверенность: ${confidenceNames[direction.current_assessment.confidence]}`} />
                <ChevronRight size={17} />
              </button>
            ))}
          </div>
        </article>

        <article className="development-panel direction-detail">
          <div className="panel-heading">
            <div><span className="eyebrow">Выбранное направление</span><h3>{selectedDirection.name}</h3></div>
            <button type="button" className="secondary-button compact" onClick={() => setAssessmentOpen(true)}>
              <Save size={16} /> Новая оценка
            </button>
          </div>
          <div className="direction-rating">
            <div className={`large-score ${scoreClass(selectedDirection.current_assessment.score)}`}>
              <strong>{selectedDirection.current_assessment.score.toFixed(1)}</strong><span>из 5</span>
            </div>
            <div className="rating-context">
              <span className={`quality-chip confidence-${selectedDirection.current_assessment.confidence.toLowerCase()}`}>
                Уверенность: {confidenceNames[selectedDirection.current_assessment.confidence]}
              </span>
              <span className="quality-chip neutral">{criticalityNames[selectedDirection.criticality]}</span>
              <small>Оценок в истории: {selectedDirection.assessment_count}</small>
            </div>
          </div>
          <dl className="direction-facts">
            <div><dt>Правило</dt><dd>{selectedDirection.rule}</dd></div>
            <div><dt>Показатель</dt><dd>{selectedDirection.metric}</dd></div>
            <div><dt>Основание оценки</dt><dd>{selectedDirection.current_assessment.evidence}</dd></div>
            <div><dt>Следующий шаг</dt><dd>{selectedDirection.current_assessment.next_step}</dd></div>
          </dl>
          <details className="assessment-history">
            <summary><History size={16} /> История оценок ({assessments.length})</summary>
            <div>
              {assessments.map((assessment) => (
                <article key={assessment.id}>
                  <strong>{assessment.score.toFixed(1)} · {confidenceNames[assessment.confidence]} уверенность</strong>
                  <span>{formatDate(assessment.created_at)} · {assessment.author}</span>
                  <p>{assessment.evidence}</p>
                </article>
              ))}
            </div>
          </details>
        </article>
      </div>

      <article className="development-panel work-items-panel">
        <div className="panel-heading work-heading">
          <div><span className="eyebrow">Работы и гипотезы</span><h3>{showAllItems ? "По всем направлениям" : selectedDirection.name}</h3></div>
          <div className="heading-actions">
            <label className="toggle-label"><input type="checkbox" checked={showAllItems} onChange={(event) => setShowAllItems(event.target.checked)} />Показать все</label>
            <button type="button" className="primary-button compact" onClick={openNewItem}><Plus size={17} /> Добавить</button>
          </div>
        </div>
        {visibleItems.length ? (
          <div className="work-item-list">
            {visibleItems.map((item) => {
              const direction = allDirections.find((row) => row.key === item.direction_key);
              return (
                <article className={`work-item status-${item.status.toLowerCase()}`} key={item.id}>
                  <div className="work-item-main">
                    <div className="work-item-labels">
                      <span>{kindNames[item.kind]}</span>
                      {item.external_key && <span className="reference-label">{item.external_key}</span>}
                      <span className={`priority-label priority-${item.priority.toLowerCase()}`}>{priorityNames[item.priority]}</span>
                    </div>
                    <h4>{item.title}</h4>
                    <p><strong>Ожидаемый результат:</strong> {item.expected_metric}</p>
                    {showAllItems && <small>{direction?.block_name} · {direction?.name}</small>}
                  </div>
                  <div className="work-item-state">
                    <label htmlFor={`status-${item.id}`}>Состояние</label>
                    <select id={`status-${item.id}`} value={item.status} disabled={saving} onChange={(event) => void changeItemStatus(item, event.target.value as DevelopmentItemStatus)}>
                      {Object.entries(statusNames).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                    </select>
                    <button type="button" className="text-button work-edit-button" onClick={() => openItem(item)}>Открыть запись</button>
                    {item.next_step && <span>{item.next_step}</span>}
                  </div>
                </article>
              );
            })}
          </div>
        ) : (
          <div className="work-items-empty"><ClipboardList size={24} /><p>По этому направлению работ пока нет.</p></div>
        )}
      </article>

      {assessmentOpen && (
        <div className="development-dialog-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && setAssessmentOpen(false)}>
          <form className="development-dialog" onSubmit={submitAssessment}>
            <div className="dialog-heading"><div><span className="eyebrow">История не перезаписывается</span><h3>Оценить: {selectedDirection.name}</h3></div><button type="button" className="icon-button" onClick={() => setAssessmentOpen(false)} aria-label="Закрыть"><X size={19} /></button></div>
            <div className="form-grid two-columns">
              <label>Оценка от 0 до 5<input name="score" type="number" min="0" max="5" step="0.1" defaultValue={selectedDirection.current_assessment.score} required /></label>
              <label>Уверенность<select name="confidence" defaultValue={selectedDirection.current_assessment.confidence}>{Object.entries(confidenceNames).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
            </div>
            <label>Чем подтверждена оценка<textarea name="evidence" rows={4} defaultValue={selectedDirection.current_assessment.evidence} required /></label>
            <label>Что сделать следующим<textarea name="next_step" rows={3} defaultValue={selectedDirection.current_assessment.next_step} required /></label>
            <div className="dialog-note"><AlertTriangle size={17} /> Сохранится новая запись. Предыдущие оценки останутся в истории.</div>
            <div className="dialog-actions"><button type="button" className="secondary-button" onClick={() => setAssessmentOpen(false)}>Отмена</button><button type="submit" className="primary-button" disabled={saving}><Save size={17} /> Сохранить оценку</button></div>
          </form>
        </div>
      )}

      {itemFormOpen && (
        <div className="development-dialog-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && setItemFormOpen(false)}>
          <form className="development-dialog wide" onSubmit={submitItem}>
            <div className="dialog-heading"><div><span className="eyebrow">{editingItemId ? "Рабочая запись" : "Новая запись плана"}</span><h3>{editingItemId ? "Изменить работу" : "Добавить работу"}</h3></div><button type="button" className="icon-button" onClick={() => setItemFormOpen(false)} aria-label="Закрыть"><X size={19} /></button></div>
            <div className="form-grid two-columns">
              <label>Вид<select value={itemDraft.kind} onChange={(event) => setItemDraft({ ...itemDraft, kind: event.target.value as DevelopmentItemKind })}>{Object.entries(kindNames).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
              <label>Направление<select value={itemDraft.direction_key} onChange={(event) => setItemDraft({ ...itemDraft, direction_key: event.target.value })}>{development.blocks.map((block) => <optgroup key={block.key} label={block.name}>{block.directions.map((direction) => <option key={direction.key} value={direction.key}>{direction.name}</option>)}</optgroup>)}</select></label>
            </div>
            <label>Название<input value={itemDraft.title} onChange={(event) => setItemDraft({ ...itemDraft, title: event.target.value })} required /></label>
            <label>Какой показатель должен измениться<textarea rows={2} value={itemDraft.expected_metric} onChange={(event) => setItemDraft({ ...itemDraft, expected_metric: event.target.value })} required /></label>
            <div className="form-grid two-columns">
              <label>Приоритет<select value={itemDraft.priority} onChange={(event) => setItemDraft({ ...itemDraft, priority: event.target.value as DevelopmentPriority })}>{Object.entries(priorityNames).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
              <label>Состояние<select value={itemDraft.status} onChange={(event) => setItemDraft({ ...itemDraft, status: event.target.value as DevelopmentItemStatus })}>{Object.entries(statusNames).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
            </div>
            <div className="form-grid two-columns">
              <label>Что уже известно<textarea rows={3} value={itemDraft.evidence} onChange={(event) => setItemDraft({ ...itemDraft, evidence: event.target.value })} /></label>
              <label>Как проверить результат<textarea rows={3} value={itemDraft.verification_method} onChange={(event) => setItemDraft({ ...itemDraft, verification_method: event.target.value })} /></label>
            </div>
            <label>Следующий шаг<textarea rows={2} value={itemDraft.next_step} onChange={(event) => setItemDraft({ ...itemDraft, next_step: event.target.value })} /></label>
            <label>Фактический результат<textarea rows={3} value={itemDraft.actual_result} onChange={(event) => setItemDraft({ ...itemDraft, actual_result: event.target.value })} placeholder="Обязательно при состоянии «Готово»" /></label>
            <label>Связанные записи, через запятую<input value={itemDraft.reference_codes.join(", ")} onChange={(event) => setItemDraft({ ...itemDraft, reference_codes: event.target.value.split(",").map((value) => value.trim()).filter(Boolean) })} placeholder="TASK-001, TASK-002" /></label>
            <div className="dialog-actions"><button type="button" className="secondary-button" onClick={() => setItemFormOpen(false)}>Отмена</button><button type="submit" className="primary-button" disabled={saving}><CheckCircle2 size={17} /> Сохранить</button></div>
          </form>
        </div>
      )}
    </section>
  );
}
