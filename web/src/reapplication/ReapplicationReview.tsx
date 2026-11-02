import { type FormEvent, useState } from "react";
import { loadReapplicationReview, saveReapplicationReview } from "../api";
import type { ReapplicationReview as Review } from "../types";

function draftKey(taskId: number): string {
  return `hugin.reapplication.reason.1.${taskId}`;
}

function savedDraft(taskId: number): string {
  try {
    return localStorage.getItem(draftKey(taskId)) ?? "";
  } catch {
    return "";
  }
}

function sourceLink(value: string): string | undefined {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? url.href : undefined;
  } catch {
    return undefined;
  }
}

export function ReapplicationReview({
  taskId,
  onSaved,
}: {
  taskId: number;
  onSaved: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState<Review | null>(null);
  const [reason, setReason] = useState(() => savedDraft(taskId));
  const [busy, setBusy] = useState(false);
  const [needsReload, setNeedsReload] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  async function reload(): Promise<void> {
    setOpen(true);
    setBusy(true);
    setError("");
    try {
      setPreview(await loadReapplicationReview(taskId));
      setNeedsReload(false);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Не удалось загрузить разбор.");
    } finally {
      setBusy(false);
    }
  }

  function changeReason(value: string): void {
    setReason(value);
    try {
      localStorage.setItem(draftKey(taskId), value);
    } catch {
      // Keep the current text if browser storage is unavailable.
    }
  }

  async function save(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (!preview?.review_key || !preview.can_review || needsReload || busy) return;
    setBusy(true);
    setError("");
    try {
      const updated = await saveReapplicationReview(taskId, reason.trim(), preview.review_key);
      setPreview(updated);
      setNotice(updated.allowed
        ? "Обоснование сохранено. Очередь проверит отклик перед продолжением."
        : `Обоснование сохранено. ${updated.reason}`);
      try {
        localStorage.removeItem(draftKey(taskId));
      } catch {
        // The saved server record is authoritative even without browser storage.
      }
      onSaved();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Не удалось сохранить обоснование.");
      setNeedsReload(true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="reapplication-review" aria-label="Разбор повторного отклика">
      <button type="button" className="quiet-button" disabled={busy}
        aria-expanded={open} onClick={() => open ? setOpen(false) : void reload()}>
        {open ? "Скрыть разбор" : "Разобрать повтор"}
      </button>
      {open && (
        <div>
          {error && <p role="alert">{error}</p>}
          {notice && <p role="status">{notice}</p>}
          {preview && !notice && (
            <>
              <p>{preview.reason}</p>
              <p>Прежние отклики:</p>
              <ul>{preview.previous.map(item => (
                <li key={item.application_id}>
                  <a href={sourceLink(item.source_url)} target="_blank" rel="noopener noreferrer">
                    {item.title} · отклик №{item.application_id}
                  </a>
                </li>
              ))}</ul>
              {preview.can_review ? (
                <form onSubmit={event => void save(event)}>
                  <label htmlFor={`reapplication-reason-${taskId}`}>Обоснование повторного отклика</label>
                  <p>Что существенно изменилось в роли или подтверждённом опыте и почему повтор полезен?</p>
                  <textarea id={`reapplication-reason-${taskId}`} value={reason} maxLength={5000}
                    disabled={busy} onChange={event => changeReason(event.target.value)} rows={4} />
                  <p>Между откликами — не менее 30 дней, не более двух за 90 дней.
                    Обоснование действует до 7 дней; перед отправкой остаются обычные проверки.</p>
                  <button type="submit" className="secondary-button"
                    disabled={busy || needsReload || reason.trim().length < 20}>
                    Сохранить обоснование
                  </button>
                </form>
              ) : !preview.allowed && <p>Разбор пока недоступен. Проверьте актуальность отбора и условия повтора.</p>}
            </>
          )}
          {(!preview || needsReload) && (
            <button type="button" className="secondary-button" disabled={busy} onClick={() => void reload()}>
              Обновить разбор
            </button>
          )}
        </div>
      )}
    </section>
  );
}
