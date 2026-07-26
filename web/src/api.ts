import type {
  Dashboard,
  FormDraft,
  QueueItem,
  QueueSettings,
  RejectedVacancy,
  VacancyCard,
} from "./types";

const ACCOUNT_ID = 1;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 12_000);
  try {
    const response = await fetch(path, {
      ...init,
      signal: init?.signal ?? controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...init?.headers,
      },
    });
    if (!response.ok) {
      const payload = (await response.json().catch(() => ({}))) as { detail?: string };
      throw new Error(payload.detail ?? `Сервер вернул ошибку ${response.status}`);
    }
    return (await response.json()) as T;
  } catch (reason) {
    if (reason instanceof DOMException && reason.name === "AbortError") {
      throw new Error("Сервер не ответил за 12 секунд");
    }
    throw reason;
  } finally {
    window.clearTimeout(timeout);
  }
}

export async function loadWorkspace(): Promise<{
  dashboard: Dashboard;
  queue: QueueItem[];
  forms: FormDraft[];
  rejected: RejectedVacancy[];
}> {
  const [dashboard, queue, forms, rejected] = await Promise.all([
    request<Dashboard>(`/api/dashboard?account_id=${ACCOUNT_ID}`),
    request<QueueItem[]>(`/api/queue?account_id=${ACCOUNT_ID}`),
    request<FormDraft[]>(`/api/forms?account_id=${ACCOUNT_ID}`),
    request<RejectedVacancy[]>(`/api/rejected?account_id=${ACCOUNT_ID}`),
  ]);
  return { dashboard, queue, forms, rejected };
}

export function loadVacancy(vacancyId: string): Promise<VacancyCard> {
  return request<VacancyCard>(
    `/api/vacancies/${encodeURIComponent(vacancyId)}?account_id=${ACCOUNT_ID}`,
  );
}

export async function changeQueueState(action: "pause" | "resume"): Promise<string> {
  const session = await request<{ key: string }>("/api/session");
  const result = await request<{ state: string }>(`/api/queue/${action}`, {
    method: "POST",
    headers: { "X-Hugin-Session": session.key },
  });
  return result.state;
}

export async function updateQueueSettings(
  values: QueueSettings,
): Promise<QueueSettings> {
  const session = await request<{ key: string }>("/api/session");
  return request<QueueSettings>("/api/queue/settings", {
    method: "PUT",
    headers: { "X-Hugin-Session": session.key },
    body: JSON.stringify(values),
  });
}
