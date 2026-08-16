import type {
  AiPromptValues,
  AutonomyPolicy,
  AutonomyPolicyValues,
  Communications,
  Dashboard,
  DirectionOptions,
  DirectionSettings,
  DirectionSummary,
  FormAnswerInput,
  FormDraft,
  Profile,
  QueueItem,
  QueueSettings,
  RejectedVacancy,
  ResumePreview,
  SentApplication,
  VacancyCard,
} from "./types";

const ACCOUNT_ID = 1;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 12_000);
  try {
    const headers = new Headers(init?.headers);
    if (!(init?.body instanceof FormData) && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    const response = await fetch(path, {
      ...init,
      signal: init?.signal ?? controller.signal,
      headers,
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
  autonomy: AutonomyPolicy;
  directionOptions: DirectionOptions;
  profile: Profile;
  queue: QueueItem[];
  forms: FormDraft[];
  rejected: RejectedVacancy[];
  sent: SentApplication[];
  communications: Communications;
}> {
  const forms = await reconcileForms();
  const [
    dashboard,
    autonomy,
    directionOptions,
    profile,
    queue,
    rejected,
    sent,
    communications,
  ] =
    await Promise.all([
      request<Dashboard>(`/api/dashboard?account_id=${ACCOUNT_ID}`),
      request<AutonomyPolicy>("/api/autonomy"),
      request<DirectionOptions>("/api/directions/options"),
      request<Profile>(`/api/profile?account_id=${ACCOUNT_ID}`),
      request<QueueItem[]>(`/api/queue?account_id=${ACCOUNT_ID}`),
      request<RejectedVacancy[]>(`/api/rejected?account_id=${ACCOUNT_ID}&limit=1000`),
      request<SentApplication[]>(`/api/sent?account_id=${ACCOUNT_ID}&limit=1000`),
      request<Communications>(`/api/communications?account_id=${ACCOUNT_ID}`),
    ]);
  return {
    dashboard,
    autonomy,
    directionOptions,
    profile,
    queue,
    forms,
    rejected,
    sent,
    communications,
  };
}

async function reconcileForms(): Promise<FormDraft[]> {
  const session = await request<{ key: string }>("/api/session");
  return request<FormDraft[]>(`/api/forms/reconcile?account_id=${ACCOUNT_ID}`, {
    method: "POST",
    headers: { "X-Hugin-Session": session.key },
  });
}

export function loadVacancy(vacancyId: string): Promise<VacancyCard> {
  return request<VacancyCard>(
    `/api/vacancies/${encodeURIComponent(vacancyId)}?account_id=${ACCOUNT_ID}`,
  );
}

export function loadCommunications(): Promise<Communications> {
  return request<Communications>(`/api/communications?account_id=${ACCOUNT_ID}`);
}

export async function changeQueueState(action: "pause" | "resume"): Promise<string> {
  const session = await request<{ key: string }>("/api/session");
  const result = await request<{ state: string }>(`/api/queue/${action}`, {
    method: "POST",
    headers: { "X-Hugin-Session": session.key },
  });
  return result.state;
}

export async function changeSearchState(action: "pause" | "resume"): Promise<void> {
  const session = await request<{ key: string }>("/api/session");
  await request(`/api/search/${action}`, {
    method: "POST",
    headers: { "X-Hugin-Session": session.key },
  });
}

export async function updateResourceSavingMode(enabled: boolean): Promise<void> {
  const session = await request<{ key: string }>("/api/session");
  await request("/api/background/resource-saving", {
    method: "PUT",
    headers: { "X-Hugin-Session": session.key },
    body: JSON.stringify({ enabled }),
  });
}

export async function reconcileApplication(
  taskId: number,
  status: "APPLIED" | "NOT_FOUND",
): Promise<void> {
  const session = await request<{ key: string }>("/api/session");
  await request(`/api/queue/${taskId}/reconcile?account_id=${ACCOUNT_ID}`, {
    method: "POST",
    headers: { "X-Hugin-Session": session.key },
    body: JSON.stringify({ status }),
  });
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

export async function updateAutonomyPolicy(
  values: AutonomyPolicyValues,
): Promise<AutonomyPolicy> {
  const session = await request<{ key: string }>("/api/session");
  return request<AutonomyPolicy>("/api/autonomy", {
    method: "PUT",
    headers: { "X-Hugin-Session": session.key },
    body: JSON.stringify(values),
  });
}

export async function updateDirection(
  directionId: number,
  values: DirectionSettings,
): Promise<DirectionSummary> {
  const session = await request<{ key: string }>("/api/session");
  return request<DirectionSummary>(
    `/api/directions/${directionId}?account_id=${ACCOUNT_ID}`,
    {
      method: "PUT",
      headers: { "X-Hugin-Session": session.key },
      body: JSON.stringify(values),
    },
  );
}

export async function previewResume(file: File): Promise<ResumePreview> {
  const session = await request<{ key: string }>("/api/session");
  const body = new FormData();
  body.append("file", file);
  return request<ResumePreview>(`/api/profile/resume/preview?account_id=${ACCOUNT_ID}`, {
    method: "POST",
    headers: { "X-Hugin-Session": session.key },
    body,
  });
}

export async function importResume(token: string): Promise<Profile> {
  const session = await request<{ key: string }>("/api/session");
  return request<Profile>(`/api/profile/resume/import?account_id=${ACCOUNT_ID}`, {
    method: "POST",
    headers: { "X-Hugin-Session": session.key },
    body: JSON.stringify({ token }),
  });
}

export async function reviewProfileFact(
  factId: number,
  action: "confirm" | "reject",
  permissions?: {
    allow_in_letters: boolean;
    allow_in_forms: boolean;
    allow_in_messages: boolean;
  },
): Promise<Profile> {
  const session = await request<{ key: string }>("/api/session");
  return request<Profile>(
    `/api/profile/facts/${factId}/${action}?account_id=${ACCOUNT_ID}`,
    {
      method: "POST",
      headers: { "X-Hugin-Session": session.key },
      body: action === "confirm" ? JSON.stringify(permissions) : undefined,
    },
  );
}

export async function correctProfileFact(
  factId: number,
  content: string,
  permissions: {
    allow_in_letters: boolean;
    allow_in_forms: boolean;
    allow_in_messages: boolean;
  },
): Promise<Profile> {
  const session = await request<{ key: string }>("/api/session");
  return request<Profile>(`/api/profile/facts/${factId}?account_id=${ACCOUNT_ID}`, {
    method: "PUT",
    headers: { "X-Hugin-Session": session.key },
    body: JSON.stringify({ content, ...permissions }),
  });
}

export async function saveProfileAnswer(key: string, answer: string): Promise<Profile> {
  const session = await request<{ key: string }>("/api/session");
  return request<Profile>(
    `/api/profile/questions/${encodeURIComponent(key)}?account_id=${ACCOUNT_ID}`,
    {
      method: "PUT",
      headers: { "X-Hugin-Session": session.key },
      body: JSON.stringify({ answer }),
    },
  );
}

export async function dismissProfileQuestion(key: string): Promise<Profile> {
  const session = await request<{ key: string }>("/api/session");
  return request<Profile>(
    `/api/profile/questions/${encodeURIComponent(key)}/dismiss?account_id=${ACCOUNT_ID}`,
    {
      method: "POST",
      headers: { "X-Hugin-Session": session.key },
    },
  );
}

export async function saveFormAnswers(
  formId: number,
  answers: FormAnswerInput[],
): Promise<FormDraft> {
  const session = await request<{ key: string }>("/api/session");
  return request<FormDraft>(
    `/api/forms/${formId}/answers?account_id=${ACCOUNT_ID}`,
    {
      method: "POST",
      headers: { "X-Hugin-Session": session.key },
      body: JSON.stringify({ answers }),
    },
  );
}

export async function markConversationRead(applicationId: number): Promise<Communications> {
  const session = await request<{ key: string }>("/api/session");
  return request<Communications>(
    `/api/communications/conversations/${applicationId}/read?account_id=${ACCOUNT_ID}`,
    {
      method: "POST",
      headers: { "X-Hugin-Session": session.key },
    },
  );
}

export async function markInvitationSeen(invitationId: number): Promise<Communications> {
  const session = await request<{ key: string }>("/api/session");
  return request<Communications>(
    `/api/communications/invitations/${invitationId}/seen?account_id=${ACCOUNT_ID}`,
    {
      method: "POST",
      headers: { "X-Hugin-Session": session.key },
    },
  );
}

export async function saveReplyDraft(
  applicationId: number,
  body: string,
): Promise<Communications> {
  const session = await request<{ key: string }>("/api/session");
  return request<Communications>(
    `/api/communications/conversations/${applicationId}/draft?account_id=${ACCOUNT_ID}`,
    {
      method: "PUT",
      headers: { "X-Hugin-Session": session.key },
      body: JSON.stringify({ body }),
    },
  );
}

export async function confirmReply(
  messageId: number,
  contentHash: string,
  contentVersion: number,
): Promise<Communications> {
  const session = await request<{ key: string }>("/api/session");
  return request<Communications>(
    `/api/communications/messages/${messageId}/confirm?account_id=${ACCOUNT_ID}`,
    {
      method: "POST",
      headers: { "X-Hugin-Session": session.key },
      body: JSON.stringify({
        content_hash: contentHash,
        content_version: contentVersion,
      }),
    },
  );
}

export async function updateNotificationSettings(
  windowsEnabled: boolean,
  telegramEnabled: boolean,
  emailEnabled: boolean,
  events: string[],
): Promise<Communications> {
  const session = await request<{ key: string }>("/api/session");
  return request<Communications>(
    `/api/communications/notifications?account_id=${ACCOUNT_ID}`,
    {
      method: "PUT",
      headers: { "X-Hugin-Session": session.key },
      body: JSON.stringify({
        windows_enabled: windowsEnabled,
        telegram_enabled: telegramEnabled,
        email_enabled: emailEnabled,
        events,
      }),
    },
  );
}

export async function updateAiPromptSettings(
  values: AiPromptValues,
): Promise<Communications> {
  const session = await request<{ key: string }>("/api/session");
  return request<Communications>(
    `/api/communications/ai-prompts?account_id=${ACCOUNT_ID}`,
    {
      method: "PUT",
      headers: { "X-Hugin-Session": session.key },
      body: JSON.stringify(values),
    },
  );
}

export async function updateAiModelSettings(
  model: string,
  reasoningEffort: string,
): Promise<Communications> {
  const session = await request<{ key: string }>("/api/session");
  return request<Communications>(
    `/api/communications/ai-model?account_id=${ACCOUNT_ID}`,
    {
      method: "PUT",
      headers: { "X-Hugin-Session": session.key },
      body: JSON.stringify({ model, reasoning_effort: reasoningEffort }),
    },
  );
}

export async function resetAiPromptSettings(): Promise<Communications> {
  const session = await request<{ key: string }>("/api/session");
  return request<Communications>(
    `/api/communications/ai-prompts/reset?account_id=${ACCOUNT_ID}`,
    {
      method: "POST",
      headers: { "X-Hugin-Session": session.key },
    },
  );
}
