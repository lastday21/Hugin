import { useEffect, useId, useState, type FormEvent } from "react";
import { loadCommunications, saveApplicationOutcome } from "../api";
import type { ApplicationOutcome, Communications } from "../types";

const emptyOutcome: ApplicationOutcome = {
  revision: 0, interview_at: null, interview_evidence: "",
  rejection_reason: "", rejection_evidence: "", recorded_at: null,
};

function localDate(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

function editable(outcome: ApplicationOutcome) {
  return { ...outcome, interview_at: localDate(outcome.interview_at) };
}

export function OutcomeEditor({ applicationId, outcome = emptyOutcome, onSaved }: {
  applicationId: number;
  outcome?: ApplicationOutcome;
  onSaved: (communications: Communications) => void;
}) {
  const id = useId();
  const [values, setValues] = useState(() => editable(outcome));
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [failed, setFailed] = useState(false);
  const stale = values.revision !== outcome.revision;

  useEffect(() => {
    if (!dirty) setValues(editable(outcome));
  }, [outcome, dirty]);

  function change(key: "interview_at" | "interview_evidence" | "rejection_reason" | "rejection_evidence", value: string) {
    setValues((current) => ({ ...current, [key]: value }));
    setDirty(true);
    setMessage("");
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setFailed(false);
    setMessage("");
    try {
      const result = await saveApplicationOutcome(applicationId, {
        revision: values.revision,
        interview_at: values.interview_at ? new Date(values.interview_at).toISOString() : null,
        interview_evidence: values.interview_evidence,
        rejection_reason: values.rejection_reason,
        rejection_evidence: values.rejection_evidence,
      });
      const saved = result.outcomes[applicationId] ?? emptyOutcome;
      setValues(editable(saved));
      setDirty(false);
      onSaved(result);
      setMessage("Результат сохранён в Hugin.");
    } catch (reason) {
      setFailed(true);
      setMessage(reason instanceof Error ? reason.message : "Не удалось сохранить результат.");
    } finally {
      setBusy(false);
    }
  }

  async function refreshSaved() {
    setBusy(true);
    try {
      onSaved(await loadCommunications());
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "Не удалось обновить сведения.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <details className="outcome-editor">
      <summary>Результат общения{outcome.interview_at ? " · дата согласована" : outcome.interview_evidence ? " · приглашение подтверждено" : outcome.rejection_reason ? " · причина отказа записана" : ""}</summary>
      <form onSubmit={(event) => void save(event)}>
        <p>Запишите приглашение именно на собеседование, согласованную встречу или объяснение отказа. Эти сведения остаются в Hugin; работодателю ничего не отправляется.</p>
        <fieldset disabled={busy}>
          <legend>Собеседование</legend>
          <label htmlFor={`${id}-date`}>Согласованная дата и время</label>
          <input id={`${id}-date`} type="datetime-local" value={values.interview_at}
            onChange={(event) => change("interview_at", event.target.value)} />
          <small>Часовой пояс устройства: {Intl.DateTimeFormat().resolvedOptions().timeZone}.</small>
          <label htmlFor={`${id}-interview-evidence`}>Подтверждение приглашения на собеседование</label>
          <textarea id={`${id}-interview-evidence`} rows={2} maxLength={2000}
            required={!!values.interview_at} value={values.interview_evidence}
            placeholder="Сообщение работодателя или запись о договорённости по телефону"
            onChange={(event) => change("interview_evidence", event.target.value)} />
          <small>Дату можно оставить пустой, если её ещё не согласовали. Приведите приглашение на разговор с работодателем или техническим специалистом и его источник. Анкета, тестовое задание и автоматический опрос, в том числе ГигаРекрутер, учитываются отдельно от такого приглашения.</small>
        </fieldset>
        <fieldset disabled={busy}>
          <legend>Объяснение отказа</legend>
          <label htmlFor={`${id}-reason`}>Причина, названная работодателем</label>
          <input id={`${id}-reason`} maxLength={500} value={values.rejection_reason}
            onChange={(event) => change("rejection_reason", event.target.value)} />
          <label htmlFor={`${id}-rejection-evidence`}>Слова работодателя и источник</label>
          <textarea id={`${id}-rejection-evidence`} rows={2} maxLength={2000}
            required={!!values.rejection_reason.trim()} value={values.rejection_evidence}
            onChange={(event) => change("rejection_evidence", event.target.value)} />
          <small>Если причина неизвестна, оставьте оба поля пустыми. Для исправления ошибочной записи очистите соответствующие поля и сохраните.</small>
        </fieldset>
        {stale && dirty && <p role="alert">Сохранённые сведения изменились. <button type="button" className="text-button"
          onClick={() => { setValues(editable(outcome)); setDirty(false); setMessage(""); setFailed(false); }}>Загрузить сохранённое</button></p>}
        <div className="outcome-save">
          <button type="submit" className="secondary-button" disabled={busy || !dirty || stale}>{busy ? "Сохраняем…" : "Сохранить результат"}</button>
          {message && <p role={failed ? "alert" : "status"}>{message}</p>}
          {failed && <button type="button" className="text-button" disabled={busy}
            onClick={() => void refreshSaved()}>Обновить сведения</button>}
        </div>
      </form>
    </details>
  );
}
