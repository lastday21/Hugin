import { AlertTriangle, Clock3, Hand } from "lucide-react";
import { useEffect, useState } from "react";
import type { BrowserControlStatus } from "./types";

export function BrowserControlNotice() {
  const [control, setControl] = useState<BrowserControlStatus | null>(null);

  useEffect(() => {
    let disposed = false;
    let pending = false;
    let timer: number | undefined;
    let timeout: number | undefined;
    const unavailable = () => {
      if (!disposed) setControl({ state: "unavailable", expires_at: null });
    };
    const refresh = async () => {
      const api = window.pywebview?.api;
      if (!api?.browser_control_status || disposed || pending) return;
      pending = true;
      timeout = window.setTimeout(unavailable, 5_000);
      try {
        const result = await api.browser_control_status();
        if (!disposed) setControl(result);
      } catch {
        unavailable();
      } finally {
        window.clearTimeout(timeout);
        pending = false;
      }
    };
    const start = () => {
      if (timer !== undefined || !window.pywebview?.api.browser_control_status) return;
      void refresh();
      timer = window.setInterval(() => void refresh(), 3_000);
    };
    start();
    window.addEventListener("pywebviewready", start);
    return () => {
      disposed = true;
      window.removeEventListener("pywebviewready", start);
      window.clearInterval(timer);
      window.clearTimeout(timeout);
    };
  }, []);

  if (!control || control.state === "automatic") return null;
  const ready = control.state === "ready";
  const requested = control.state === "requested";
  const Icon = ready ? Hand : requested ? Clock3 : AlertTriangle;
  const title = ready
    ? "Браузер передан для ручной работы"
    : requested
      ? "Готовим браузер к передаче управления"
      : control.state === "failed"
        ? "Не удалось передать управление браузером"
        : "Не удалось проверить управление браузером";
  const description = ready
    ? "Автоматические действия в этом браузере ждут возврата управления. Настройки процессов сохранены."
    : requested
      ? "Hugin завершает текущее действие. Передача ещё не подтверждена."
      : control.state === "failed"
        ? "Ручная работа не началась. Запрос передачи нужно завершить или повторить."
        : "Состояние передачи неизвестно. Настройки процессов не изменены.";

  return (
    <div
      className={`data-warning browser-control-notice ${ready ? "manual" : requested ? "pending" : ""}`}
      role="status"
      aria-live="polite"
    >
      <Icon size={20} aria-hidden="true" />
      <div>
        <strong>{title}</strong>
        <span>{description}</span>
        {ready && control.expires_at && (
          <span>
            До {new Date(control.expires_at).toLocaleTimeString("ru-RU", {
              hour: "2-digit", minute: "2-digit",
            })}
          </span>
        )}
      </div>
    </div>
  );
}
