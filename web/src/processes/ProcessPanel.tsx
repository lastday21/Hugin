import { useEffect, useRef, useState } from "react";
import { checkMessagesNow, saveProcessSchedule, setProcessEnabled, stopAllProcesses } from "../api";
import type { BackgroundProcesses, ProcessKey } from "../types";

const descriptions: Record<ProcessKey, string> = {
  search: "Находит карточки и загружает полные описания.",
  evaluation: "Оценивает уже загруженные вакансии, даже при выключенном поиске.",
  applications: "Готовит и отправляет отклики по завершённой оценке.",
  synchronization: "Получает сообщения и проверяет статусы откликов.",
  replies: "Готовит и отправляет разрешённые ответы. Неизвестные сведения оставляет вам.",
};
const stateNames: Record<BackgroundProcesses["processes"][number]["state"], string> = {
  disabled: "Выключен", waiting: "Ожидает", running: "Выполняется",
  stopping: "Завершает остановку", interrupted: "Работа прервана",
  blocked: "Нужно действие", error: "Ошибка",
};
function dateText(value: string | null): string {
  return value ? new Date(value).toLocaleString("ru-RU") : "Ещё не записано";
}

export function ProcessPanel({ data, loadError, onSaved, onRefresh }: {
  data: BackgroundProcesses | null;
  loadError?: string;
  onSaved: (data: BackgroundProcesses) => void;
  onRefresh: () => void;
}) {
  const [pending, setPending] = useState(false);
  const busy = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [messageInterval, setMessageInterval] = useState("");
  const [statusInterval, setStatusInterval] = useState("");
  const [scheduleDirty, setScheduleDirty] = useState(false);
  const messages = data?.synchronization.message_interval_minutes;
  const statuses = data?.synchronization.status_interval_minutes;
  useEffect(() => {
    if (!scheduleDirty && messages !== undefined && statuses !== undefined) {
      setMessageInterval(String(messages));
      setStatusInterval(String(statuses));
    }
  }, [messages, statuses, scheduleDirty]);

  async function change(action: () => Promise<BackgroundProcesses>, success: string): Promise<boolean> {
    if (busy.current) return false;
    busy.current = true;
    setPending(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await action();
      onSaved(updated);
      setNotice(success);
      return true;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось сохранить изменение");
      return false;
    } finally {
      busy.current = false;
      setPending(false);
      onRefresh();
    }
  }
  const intervalValid = [messageInterval, statusInterval].every((value) =>
    /^\d+$/.test(value) && Number(value) >= 1 && Number(value) <= 1440,
  );
  return (
    <section className="dashboard-card process-panel" aria-labelledby="process-title" aria-busy={pending}>
      <div className="process-heading">
        <div>
          <h2 id="process-title">Управление процессами</h2>
          <p>Включённые процессы выполняются по очереди.</p>
        </div>
        <button type="button" className="secondary-button" disabled={pending}
          onClick={() => void change(stopAllProcesses, "Все процессы выключены. Текущий ход завершает остановку.")}>
          Остановить всё
        </button>
      </div>
      {loadError && <p role="alert">Не удалось обновить состояние: {loadError}. Показаны последние полученные сведения.</p>}
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      {!data ? <p>{loadError ? "Состояние процессов неизвестно." : "Загружаем состояние процессов…"}</p> : <>
        <div className="process-list">
          {data.processes.map((process) => <article className={`process-row process-${process.state}`} key={process.key}>
            <div>
              <h3>{process.name}</h3>
              <p><strong>{stateNames[process.state]}</strong>{["error", "blocked", "interrupted", "stopping"].includes(process.state) && <> · {process.reason}</>}</p>
              <details>
                <summary>Подробности</summary>
                <p>{descriptions[process.key]}</p>
                <p>{process.reason}</p>
                <p>Начало: {dateText(process.last_started_at)}<br />Завершение: {dateText(process.last_finished_at)}</p>
              </details>
            </div>
            <label className="process-toggle">
              <input type="checkbox" role="switch" checked={process.enabled} disabled={pending || Boolean(loadError)}
                aria-label={process.name}
                onChange={(event) => {
                  const enabled = event.target.checked;
                  void change(() => setProcessEnabled(process.key, enabled), `${process.name}: ${enabled ? "включено" : "выключено"}`);
                }} />
              <span>{process.enabled ? "Включён" : "Выключен"}</span>
            </label>
          </article>)}
        </div>
        <details className="process-settings"><summary>Расписание проверок и общий остаток вакансий</summary>
        <form className="process-schedule" onSubmit={(event) => {
          event.preventDefault();
          if (!intervalValid || !scheduleDirty) return;
          void change(() => saveProcessSchedule(Number(messageInterval), Number(statusInterval)), "Частота проверок сохранена")
            .then((saved) => { if (saved) setScheduleDirty(false); });
        }}>
          <h3>Частота проверок</h3>
          <div className="process-schedule-fields">
            <label>Сообщения, каждые (минуты)
              <input type="number" min="1" max="1440" step="1" required value={messageInterval} disabled={pending}
                onChange={(event) => { setMessageInterval(event.target.value); setScheduleDirty(true); }} />
            </label>
            <label>Статусы откликов, каждые (минуты)
              <input type="number" min="1" max="1440" step="1" required value={statusInterval} disabled={pending}
                onChange={(event) => { setStatusInterval(event.target.value); setScheduleDirty(true); }} />
            </label>
            <button type="submit" className="secondary-button" disabled={pending || !intervalValid || !scheduleDirty || Boolean(loadError)}>Сохранить частоту</button>
            <button type="button" className="secondary-button" disabled={pending || data.synchronization.check_now_pending || Boolean(loadError)}
              onClick={() => void change(checkMessagesNow, "Разовая проверка поставлена в очередь")}>
              {data.synchronization.check_now_pending ? "Проверка ожидает своей очереди" : "Проверить сейчас"}
            </button>
          </div>
          <p>Разовая проверка сообщений и статусов работает и при выключенных регулярных проверках. Ответы сама не включает.</p>
        </form>
        <section className="process-funnel" aria-labelledby="funnel-title">
          <h3 id="funnel-title">Найденные вакансии: {data.funnel.total.toLocaleString("ru-RU")}</h3>
          <p>{data.funnel.scope} Каждая вакансия учтена в одной стадии.</p>
          <dl>{data.funnel.stages.map((stage) => <div key={stage.key}><dt>{stage.name}</dt><dd>{stage.count.toLocaleString("ru-RU")}</dd></div>)}</dl>
        </section>
        <details className="process-search">
          <summary>Последнее чтение выдачи hh.ru</summary>
          {data.last_search ? <>
            <dl>
              <div><dt>Время</dt><dd>{dateText(data.last_search.observed_at)}</dd></div>
              <div><dt>Запрос</dt><dd>{data.last_search.query ?? "Не записан"}</dd></div>
              <div><dt>Регион</dt><dd>{data.last_search.region ?? "Не записан"}</dd></div>
              <div><dt>Страница</dt><dd>{data.last_search.page ?? "Не записана"}</dd></div>
              <div><dt>Найдено hh.ru по этому запросу</dt><dd>{data.last_search.found?.toLocaleString("ru-RU") ?? "Неизвестно"}</dd></div>
              <div><dt>Предел страниц обхода</dt><dd>{data.last_search.coverage_page_limit ?? "Не записан"}</dd></div>
              <div><dt>Охват</dt><dd>{data.last_search.coverage_exhausted === true ? "Достигнут конец выдачи этого запроса" : data.last_search.coverage_exhausted === false ? "Конец выдачи не подтверждён" : "Неизвестен"}</dd></div>
            </dl>
            <p>Это наблюдение одного запроса в указанное время. Числа разных запросов могут пересекаться; они не входят в местный остаток.</p>
          </> : <p>Чтение выдачи ещё не подтверждено. Общий объём доступных на hh.ru вакансий неизвестен.</p>}
        </details>
        </details>
      </>}
    </section>
  );
}
