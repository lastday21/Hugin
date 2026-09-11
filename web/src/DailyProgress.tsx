import { useEffect, useRef, useState } from "react";
import { fetchProgressVacancies } from "./api";
import type { Dashboard, ProgressVacancies } from "./types";

function dateText(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString("ru-RU") : "Неизвестно";
}
const countText = (value: number | null | undefined) => value == null ? "Не загружено" : value.toLocaleString("ru-RU");

export function DailyProgress({ dashboard, onOpenVacancy }: {
  dashboard: Dashboard;
  onOpenVacancy: (id: string) => Promise<void>;
}) {
  const progress = dashboard.day_progress;
  const [selected, setSelected] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const [page, setPage] = useState<ProgressVacancies | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [opening, setOpening] = useState<string | null>(null);
  const openingRef = useRef(false);
  const listHeading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    setPage(null);
    void fetchProgressVacancies(selected, offset, controller.signal).then((result) => {
      if (!controller.signal.aborted) setPage(result);
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Не удалось загрузить список");
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [selected, offset, attempt]);
  useEffect(() => { if (selected) listHeading.current?.focus(); }, [selected]);

  async function openVacancy(id: string) {
    if (openingRef.current) return;
    openingRef.current = true;
    setOpening(id);
    try { await onOpenVacancy(id); }
    catch { setError("Не удалось открыть карточку вакансии. Попробуйте ещё раз."); }
    finally { openingRef.current = false; setOpening(null); }
  }
  const stage = progress?.stages.find((item) => item.key === selected);
  return <section className="dashboard-card daily-card" aria-labelledby="daily-widget-title">
    <div className="card-heading"><div>
      <h3 id="daily-widget-title">Сегодня</h3>
      <p>С начала суток · {dashboard.day_timezone ?? "Часовой пояс неизвестен"}</p>
    </div></div>
    <div className="daily-summary-values">
      <div><strong>{countText(dashboard.found_today)}</strong><small>вакансий найдено</small></div>
      <div><strong>{countText(dashboard.viewed_today)}</strong><small>описаний прочитано</small></div>
      <div><strong>{countText(dashboard.confirmed_applied_today)}</strong><small>откликов отправлено за день</small></div>
      <div><strong>{countText(dashboard.replies_sent_today)}</strong><small>ответов работодателям за день</small></div>
    </div>
    <div className="day-progress-heading"><h3>Из прочитанных сегодня</h3>
      <span>{progress ? `${progress.total} вакансий` : "Сведения не загружены"}</span></div>
    <p className="card-note">Текущее состояние этих вакансий. Каждая учтена один раз. Нажмите этап, чтобы увидеть вакансии. Отправленный отклик мог быть сделан раньше сегодняшнего дня.</p>
    {progress ? <>
      <div className="day-stages">{progress.stages.map((item) => <button type="button" key={item.key}
        className={`day-stage ${selected === item.key ? "selected" : ""}`}
        disabled={item.count === 0} aria-expanded={selected === item.key} aria-controls="day-progress-list"
        onClick={() => { setSelected(selected === item.key ? null : item.key); setOffset(0); }}>
        <span><strong>{item.count.toLocaleString("ru-RU")}</strong> {item.name}</span>
        <small>{item.description}</small>
      </button>)}</div>
      {progress.total === 0 && <p>Сегодня полные описания ещё не прочитаны. После чтения здесь появятся результаты оценки и дальнейшие действия.</p>}
      <p className="card-note">Состояние на {dateText(progress.observed_at)}.
        {progress.oldest_pending_at && <> Самая ранняя ожидающая вакансия прочитана {dateText(progress.oldest_pending_at)}.</>}</p>
    </> : <p role="status">Распределение по этапам не получено. Обновите состояние; если сведения не появятся, требуется обновление приложения.</p>}
    {selected && <section id="day-progress-list" className="day-progress-list" aria-busy={loading}>
      <div className="process-heading"><h4 ref={listHeading} tabIndex={-1}>{stage?.name ?? "Вакансии выбранного этапа"}</h4>
        <button className="text-button" type="button" onClick={() => {
          document.querySelector<HTMLButtonElement>(".day-stage.selected")?.focus(); setSelected(null);
        }}>Закрыть список</button></div>
      {loading && <p role="status">Загружаем вакансии…</p>}
      {error && <div role="alert"><p>{error}</p><button type="button" className="secondary-button" onClick={() => setAttempt((value) => value + 1)}>Повторить загрузку</button></div>}
      {page && <>
        <p className="card-note">Прочитаны с {dateText(page.since)}. Состояние списка на {dateText(page.observed_at)}.</p>
        {page.since !== progress?.since && <p role="status">Период изменился. Список показан за новые сутки; обновите сводку.</p>}
        <ul className="day-vacancies">{page.items.map((item) => <li key={item.vacancy_id}>
          <button type="button" disabled={opening !== null} onClick={() => void openVacancy(item.vacancy_id)}>
            <strong>{opening === item.vacancy_id ? "Открываем…" : item.title}</strong><span>{item.company}</span>
            <small>{item.description} · Прочитана {dateText(item.read_at)}</small>
          </button></li>)}</ul>
        {page.items.length === 0 && <p>Вакансий на этой странице больше нет: их состояние могло измениться. Обновите список или вернитесь к первой странице.</p>}
        <div className="day-pagination">
          <button type="button" className="secondary-button" disabled={offset === 0 || loading} onClick={() => setOffset(Math.max(0, offset - 25))}>Назад</button>
          <span>{page.items.length ? `${page.offset + 1}–${page.offset + page.items.length} из ${page.total}` : `Всего: ${page.total}`}</span>
          <button type="button" className="secondary-button" disabled={page.offset + page.limit >= page.total || loading} onClick={() => setOffset(offset + 25)}>Далее</button>
          <button type="button" className="text-button" onClick={() => { setOffset(0); setAttempt((value) => value + 1); }}>Обновить список</button>
        </div>
      </>}
    </section>}
    <details className="day-limit"><summary>Дневное ограничение: {countText(dashboard.applied_today)} из {countText(dashboard.daily_limit)}</summary>
      <p>В ограничении учитываются и попытки с неизвестным результатом. Они не входят в число подтверждённых отправок.</p>
      <p>Повторное чтение одной вакансии не увеличивает итог. Найденные в выдаче карточки могут ещё ожидать загрузки полного описания.</p>
    </details>
  </section>;
}
