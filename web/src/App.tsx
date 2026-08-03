import {
  AlertTriangle,
  Bell,
  BriefcaseBusiness,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CirclePause,
  CirclePlay,
  ExternalLink,
  FileQuestion,
  Gauge,
  Home,
  Inbox,
  ListFilter,
  MessageSquare,
  RefreshCw,
  RotateCcw,
  Search,
  Settings,
  SlidersHorizontal,
  Sparkles,
  Upload,
  UserRound,
  X,
} from "lucide-react";
import {
  type FormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  type RefObject,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  changeQueueState,
  changeSearchState,
  dismissProfileQuestion,
  importResume,
  loadCommunications,
  loadVacancy,
  loadWorkspace,
  markConversationRead,
  markInvitationSeen,
  previewResume,
  reconcileApplication,
  reviewProfileFact,
  saveReplyDraft,
  saveProfileAnswer,
  resetAiPromptSettings,
  updateAiModelSettings,
  updateAiPromptSettings,
  updateNotificationSettings,
  updateDirection,
  updateQueueSettings,
  updateResourceSavingMode,
  confirmReply,
} from "./api";
import {
  dashboardWidgetDefinitions,
  defaultDashboardWidgets,
  loadDashboardWidgets,
  saveDashboardWidgets,
  type DashboardWidget,
} from "./dashboardPreferences";
import type {
  AiModelSettings,
  AiPromptSettings,
  AiPromptValues,
  Communications,
  Conversation,
  Dashboard,
  DirectionOptions,
  DirectionSettings,
  DirectionSummary,
  EmploymentForm,
  FormDraft,
  NotificationSettings,
  Profile,
  ProfileFact,
  ProfileQuestion,
  QueueItem,
  QueueSettings,
  RejectedVacancy,
  ResumePreview,
  SearchRegion,
  SentApplication,
  SystemState,
  VacancyCard,
  WorkFormat,
} from "./types";

type View =
  | "dashboard"
  | "vacancies"
  | "attention"
  | "communications"
  | "profile"
  | "settings";
type VacancyTab = "queue" | "sent" | "rejected";
type AttentionTab = "input" | "review";
type Toast = { kind: "success" | "error"; message: string };

interface Workspace {
  dashboard: Dashboard;
  directionOptions: DirectionOptions;
  profile: Profile;
  queue: QueueItem[];
  forms: FormDraft[];
  rejected: RejectedVacancy[];
  sent: SentApplication[];
  communications: Communications;
}

const navigation: {
  id: View;
  label: string;
  icon: typeof Home;
}[] = [
  { id: "dashboard", label: "Главная", icon: Home },
  { id: "vacancies", label: "Вакансии", icon: BriefcaseBusiness },
  { id: "attention", label: "Требует внимания", icon: Bell },
  { id: "communications", label: "Общение", icon: MessageSquare },
  { id: "profile", label: "Профиль", icon: UserRound },
  { id: "settings", label: "Настройки", icon: Settings },
];

const viewTitles: Record<View, { title: string; description: string }> = {
  dashboard: {
    title: "Главная",
    description: "Состояние очереди и ближайшие действия",
  },
  vacancies: {
    title: "Вакансии",
    description: "Очередь откликов и отклонённые варианты",
  },
  attention: {
    title: "Требует внимания",
    description: "Только то, где нужно ваше решение",
  },
  communications: {
    title: "Общение",
    description: "Сообщения работодателей и приглашения",
  },
  profile: {
    title: "Профиль",
    description: "Резюме, подтверждённые сведения и частые ответы",
  },
  settings: {
    title: "Настройки",
    description: "Вид главной и текущие правила работы",
  },
};

const stateNames: Record<string, string> = {
  DISCOVERED: "Найдена",
  ANALYZED: "Проверена",
  QUEUED: "В очереди",
  CLOSED: "Закрыта",
  PENDING: "Ожидает",
  RUNNING: "В работе",
  RETRY_SCHEDULED: "Повтор позже",
  REVIEW_REQUIRED: "Нужно проверить",
  INPUT_REQUIRED: "Нужно заполнить",
  SKIPPED: "Пропущено",
  UNKNOWN_RESULT: "Нужно уточнить",
  COMPLETED: "Готово",
  FILTERED_OUT: "Не подходит",
  READY: "Готово",
  FAILED: "Ошибка",
  SENT: "Отправлено",
  CONFIRMED: "Подтверждено",
  RECEIVED: "Получено",
  PREPARING: "Готовится",
  SCHEDULED: "Запланировано",
  DRAFT: "Черновик",
  INVALIDATED: "Устарело",
  APPLY_INTENT: "Начат отклик",
  APPLIED: "Отклик отправлен",
  VIEWED: "Резюме просмотрено",
  INVITED: "Приглашение",
  REJECTED: "Отказ",
  STATE_CHANGED: "Состояние изменено",
};

const sourceNames: Record<string, string> = {
  PROFILE: "из профиля",
  BANK: "из сохранённых ответов",
  YANDEXGPT: "предложено помощником",
  USER: "введено вами",
};

function readableError(reason: unknown): string {
  return reason instanceof Error ? reason.message : "Не удалось выполнить действие";
}

function formatDate(value: string | null, includeDate = false): string {
  if (!value) return "Не назначено";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Время не указано";
  const today = new Date();
  const showDate =
    includeDate ||
    date.getFullYear() !== today.getFullYear() ||
    date.getMonth() !== today.getMonth() ||
    date.getDate() !== today.getDate();
  return new Intl.DateTimeFormat("ru-RU", {
    day: showDate ? "numeric" : undefined,
    month: showDate ? "short" : undefined,
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function plural(value: number, one: string, few: string, many: string): string {
  const lastTwo = value % 100;
  const last = value % 10;
  if (lastTwo >= 11 && lastTwo <= 14) return `${value} ${many}`;
  if (last === 1) return `${value} ${one}`;
  if (last >= 2 && last <= 4) return `${value} ${few}`;
  return `${value} ${many}`;
}

function formatDelayRange(minSeconds: number, maxSeconds: number): string {
  if (maxSeconds < 120) return `${minSeconds}–${maxSeconds} сек`;
  return `${Math.round(minSeconds / 60)}–${Math.round(maxSeconds / 60)} мин`;
}

function initials(label: string): string {
  const parts = label.trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "HH";
  return parts
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase();
}

function visibleReasons(reasons: string[]): string[] {
  return reasons
    .map((reason) => reason.trim())
    .filter(
      (reason) =>
        reason.length > 0 &&
        !/^[\p{L}_ -]+:\s*[\d._-]+$/u.test(reason) &&
        !/^[A-Z0-9_:-]+$/.test(reason),
    );
}

function useDialog(
  open: boolean,
  dialogRef: RefObject<HTMLElement | null>,
  initialFocusRef: RefObject<HTMLElement | null>,
  onClose: () => void,
  returnFocusRef?: RefObject<HTMLElement | null>,
): void {
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) return;

    const previousFocus =
      returnFocusRef?.current ??
      (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.requestAnimationFrame(() => initialFocusRef.current?.focus());

    function onKeyDown(event: KeyboardEvent): void {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;

      const focusable = Array.from(
        dialogRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), a[href], input:not([disabled]), details > summary, [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => element.offsetParent !== null);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];

      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, [dialogRef, initialFocusRef, open, returnFocusRef]);
}

export default function App() {
  const [view, setView] = useState<View>("dashboard");
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [selectedVacancy, setSelectedVacancy] = useState<VacancyCard | null>(null);
  const [vacancyLoading, setVacancyLoading] = useState(false);
  const [preferencesOpen, setPreferencesOpen] = useState(false);
  const [widgets, setWidgets] = useState<DashboardWidget[]>(loadDashboardWidgets);
  const [toast, setToast] = useState<Toast | null>(null);
  const pageTitleRef = useRef<HTMLHeadingElement>(null);
  const vacancyOpenerRef = useRef<HTMLElement>(null);

  const refresh = useCallback(async (initial = false) => {
    if (initial) setInitialLoading(true);
    else setRefreshing(true);
    try {
      const nextWorkspace = await loadWorkspace();
      setWorkspace(nextWorkspace);
      setLastUpdated(new Date());
      setError(null);
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setInitialLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void refresh(true);
    const timer = window.setInterval(() => void refresh(false), 30_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    pageTitleRef.current?.focus();
  }, [view]);

  useEffect(() => {
    if (!toast || toast.kind === "error") return;
    const timer = window.setTimeout(() => setToast(null), 5_000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const showToast = useCallback((nextToast: Toast) => setToast(nextToast), []);

  const applyQueueSettings = useCallback(
    (settings: QueueSettings) => {
      setWorkspace((current) =>
        current
          ? {
              ...current,
              dashboard: {
                ...current.dashboard,
                ...settings,
                remaining_today: Math.max(
                  settings.daily_limit - current.dashboard.applied_today,
                  0,
                ),
              },
            }
          : current,
      );
      void refresh(false);
    },
    [refresh],
  );

  const applyDirectionSettings = useCallback(
    (direction: DirectionSummary) => {
      setWorkspace((current) =>
        current
          ? {
              ...current,
              dashboard: {
                ...current.dashboard,
                directions: current.dashboard.directions.map((item) =>
                  item.id === direction.id ? direction : item,
                ),
              },
            }
          : current,
      );
      void refresh(false);
    },
    [refresh],
  );

  const applyProfile = useCallback((profile: Profile) => {
    setWorkspace((current) => (current ? { ...current, profile } : current));
  }, []);

  const reconcileUnknown = useCallback(
    async (taskId: number, status: "APPLIED" | "NOT_FOUND") => {
      try {
        await reconcileApplication(taskId, status);
        await refresh(false);
        showToast({
          kind: "success",
          message:
            status === "APPLIED"
              ? "Отклик добавлен в историю отправленных"
              : "Отсутствие отклика сохранено; автоматического повтора не будет",
        });
      } catch (reason) {
        showToast({ kind: "error", message: readableError(reason) });
      }
    },
    [refresh, showToast],
  );

  const toggleWidget = useCallback(
    (widget: DashboardWidget) => {
      setWidgets((current) => {
        const next = current.includes(widget)
          ? current.filter((item) => item !== widget)
          : [...current, widget];
        saveDashboardWidgets(next);
        return next;
      });
    },
    [],
  );

  const resetWidgets = useCallback(() => {
    setWidgets(defaultDashboardWidgets);
    saveDashboardWidgets(defaultDashboardWidgets);
  }, []);

  const openVacancy = useCallback(
    async (vacancyId: string) => {
      if (vacancyLoading) return;
      if (document.activeElement instanceof HTMLElement) {
        vacancyOpenerRef.current = document.activeElement;
      }
      setVacancyLoading(true);
      try {
        setSelectedVacancy(await loadVacancy(vacancyId));
      } catch (reason) {
        showToast({ kind: "error", message: readableError(reason) });
      } finally {
        setVacancyLoading(false);
      }
    },
    [showToast, vacancyLoading],
  );

  const formsCount = workspace?.forms.length ?? 0;
  const profileAttentionCount = workspace
    ? workspace.profile.facts.filter((fact) => fact.state === "PENDING").length +
      workspace.profile.questions.filter((question) => question.state === "PENDING").length
    : 0;
  const accountLabel = workspace?.dashboard.account_label ?? "Аккаунт hh.ru";
  const currentTitle = viewTitles[view];

  return (
    <div className="app-shell" aria-busy={initialLoading}>
      <aside className="sidebar">
        <div className="brand" aria-label="Hugin">
          <span className="brand-mark" aria-hidden="true">
            H
          </span>
          <div>
            <strong>Hugin</strong>
            <span>Поиск работы</span>
          </div>
        </div>

        <nav className="main-navigation" aria-label="Основные разделы">
          {navigation.map((item) => {
            const Icon = item.icon;
            const badge =
              item.id === "vacancies"
                ? workspace?.queue.length
                : item.id === "attention"
                  ? formsCount
                  : item.id === "communications"
                    ? (workspace?.communications.unread_messages ?? 0) +
                      (workspace?.communications.unseen_invitations ?? 0)
                  : item.id === "profile"
                    ? profileAttentionCount
                  : undefined;
            return (
              <button
                key={item.id}
                type="button"
                className={view === item.id ? "nav-item active" : "nav-item"}
                aria-label={item.label}
                aria-current={view === item.id ? "page" : undefined}
                onClick={() => setView(item.id)}
              >
                <Icon size={20} aria-hidden="true" />
                <span>{item.label}</span>
                {!!badge && <span className="nav-badge">{badge}</span>}
              </button>
            );
          })}
        </nav>

        <div className="sidebar-account">
          <span
            className={workspace && !error ? "connection-dot online" : "connection-dot"}
            aria-hidden="true"
          />
          <div>
            <strong>{accountLabel}</strong>
            <span>{workspace && !error ? "Данные актуальны" : "Нет обновления"}</span>
          </div>
        </div>
      </aside>

      <div className="workspace">
        <header className="topbar">
          <div className="page-heading">
            <h1 ref={pageTitleRef} tabIndex={-1}>
              {currentTitle.title}
            </h1>
            <p>{currentTitle.description}</p>
          </div>
          <div className="topbar-actions">
            <span className="updated-at">
              {lastUpdated
                ? `Обновлено в ${lastUpdated.toLocaleTimeString("ru-RU", {
                    hour: "2-digit",
                    minute: "2-digit",
                  })}`
                : "Данные ещё не получены"}
            </span>
            <button
              type="button"
              className="icon-button"
              onClick={() => void refresh(false)}
              disabled={refreshing}
              aria-label="Обновить данные"
            >
              <RefreshCw
                size={19}
                className={refreshing ? "spin" : undefined}
                aria-hidden="true"
              />
            </button>
            <span className="avatar" aria-label={`Аккаунт: ${accountLabel}`}>
              {initials(accountLabel)}
            </span>
          </div>
        </header>

        <main className="main-content">
          {error && (
            <div className="data-warning" role="alert">
              <AlertTriangle size={20} aria-hidden="true" />
              <div>
                <strong>
                  {workspace
                    ? "Не удалось обновить данные"
                    : "Не удалось подключиться к программе"}
                </strong>
                <span>{error}</span>
              </div>
              <button type="button" className="text-button" onClick={() => void refresh(false)}>
                Повторить
              </button>
            </div>
          )}

          {initialLoading && !workspace ? (
            <LoadingState />
          ) : workspace ? (
            <>
              {view === "dashboard" && (
                <DashboardView
                  workspace={workspace}
                  widgets={widgets}
                  onOpenVacancy={openVacancy}
                  onOpenAttention={() => setView("attention")}
                  onOpenVacancies={() => setView("vacancies")}
                  onOpenPreferences={() => setPreferencesOpen(true)}
                  onRefresh={() => void refresh(false)}
                  onToast={showToast}
                />
              )}
              {view === "vacancies" && (
                <VacanciesView
                  queue={workspace.queue}
                  sent={workspace.sent}
                  rejected={workspace.rejected}
                  loading={vacancyLoading}
                  onOpenVacancy={openVacancy}
                  onReconcile={reconcileUnknown}
                />
              )}
              {view === "attention" && (
                <AttentionView forms={workspace.forms} onToast={showToast} />
              )}
              {view === "communications" && (
                <CommunicationsView
                  communications={workspace.communications}
                  onChanged={(communications) =>
                    setWorkspace((current) =>
                      current ? { ...current, communications } : current,
                    )
                  }
                  onToast={showToast}
                />
              )}
              {view === "profile" && (
                <ProfileView
                  profile={workspace.profile}
                  onProfileChanged={applyProfile}
                  onToast={showToast}
                />
              )}
              {view === "settings" && (
                <SettingsView
                  dashboard={workspace.dashboard}
                  directionOptions={workspace.directionOptions}
                  notificationSettings={workspace.communications.notification_settings}
                  aiModelSettings={workspace.communications.ai_model_settings}
                  aiPromptSettings={workspace.communications.ai_prompt_settings}
                  widgets={widgets}
                  onToggleWidget={toggleWidget}
                  onResetWidgets={resetWidgets}
                  onSettingsSaved={applyQueueSettings}
                  onDirectionSaved={applyDirectionSettings}
                  onRefresh={() => void refresh(false)}
                  onNotificationsSaved={(communications) =>
                    setWorkspace((current) =>
                      current ? { ...current, communications } : current,
                    )
                  }
                  onToast={showToast}
                />
              )}
            </>
          ) : (
            <EmptyState
              icon={<AlertTriangle size={26} />}
              title="Данные недоступны"
              description="Проверьте, что приложение запущено, и повторите попытку."
              action={
                <button type="button" className="primary-button" onClick={() => void refresh(false)}>
                  Попробовать снова
                </button>
              }
            />
          )}
        </main>
      </div>

      <VacancyDrawer
        vacancy={selectedVacancy}
        returnFocusRef={vacancyOpenerRef}
        onClose={() => setSelectedVacancy(null)}
        onToast={showToast}
      />
      <PreferencesDialog
        open={preferencesOpen}
        widgets={widgets}
        onClose={() => setPreferencesOpen(false)}
        onToggle={toggleWidget}
        onReset={resetWidgets}
      />
      {toast && <ToastMessage toast={toast} onClose={() => setToast(null)} />}
    </div>
  );
}

function LoadingState() {
  return (
    <div className="loading-state" role="status">
      <RefreshCw className="spin" size={26} aria-hidden="true" />
      <span>Загружаем рабочие данные…</span>
    </div>
  );
}

function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon: ReactNode;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <span className="empty-icon" aria-hidden="true">
        {icon}
      </span>
      <h2>{title}</h2>
      <p>{description}</p>
      {action}
    </div>
  );
}

function DashboardView({
  workspace,
  widgets,
  onOpenVacancy,
  onOpenAttention,
  onOpenVacancies,
  onOpenPreferences,
  onRefresh,
  onToast,
}: {
  workspace: Workspace;
  widgets: DashboardWidget[];
  onOpenVacancy: (vacancyId: string) => Promise<void>;
  onOpenAttention: () => void;
  onOpenVacancies: () => void;
  onOpenPreferences: () => void;
  onRefresh: () => void;
  onToast: (toast: Toast) => void;
}) {
  const { dashboard, queue, forms } = workspace;
  const [changingState, setChangingState] = useState(false);
  const [changingSearch, setChangingSearch] = useState(false);

  async function controlQueue(action: "pause" | "resume"): Promise<void> {
    if (changingState) return;
    setChangingState(true);
    try {
      await changeQueueState(action);
      onToast({
        kind: "success",
        message:
          action === "pause"
            ? "Новые отклики приостановлены"
            : "Отправка новых откликов продолжена",
      });
      onRefresh();
    } catch (reason) {
      onToast({ kind: "error", message: readableError(reason) });
    } finally {
      setChangingState(false);
    }
  }

  async function controlSearch(action: "pause" | "resume"): Promise<void> {
    if (changingSearch) return;
    setChangingSearch(true);
    try {
      await changeSearchState(action);
      onToast({
        kind: "success",
        message:
          action === "pause"
            ? "Новые поиски остановлены; текущая проверка завершит безопасный шаг"
            : "Поиск вакансий возобновлён",
      });
      onRefresh();
    } catch (reason) {
      onToast({ kind: "error", message: readableError(reason) });
    } finally {
      setChangingSearch(false);
    }
  }

  const system = systemPresentation(dashboard.system_state, queue.length);
  const SystemIcon = system.icon;

  return (
    <div className="page-stack">
      <section className={`system-card ${system.tone}`} aria-labelledby="system-title">
        <div className="system-icon" aria-hidden="true">
          <SystemIcon size={26} />
        </div>
        <div className="system-copy">
          <span className="eyebrow">Состояние программы</span>
          <h2 id="system-title">{system.title}</h2>
          <p>{system.description}</p>
          {dashboard.system_state === "RUNNING" && dashboard.next_apply_at && (
            <span className="next-action">
              Следующее действие — {formatDate(dashboard.next_apply_at)}
            </span>
          )}
        </div>
        <div className="system-action">
          <button
            type="button"
            className="secondary-button"
            disabled={changingSearch}
            onClick={() =>
              void controlSearch(dashboard.search_enabled ? "pause" : "resume")
            }
          >
            <Search size={19} aria-hidden="true" />
            {changingSearch
              ? "Сохраняем…"
              : dashboard.search_enabled
                ? "Остановить поиск"
                : "Возобновить поиск"}
          </button>
          {dashboard.system_state === "RUNNING" && (
            <button
              type="button"
              className="secondary-button"
              disabled={changingState}
              onClick={() => void controlQueue("pause")}
            >
              <CirclePause size={19} aria-hidden="true" />
              {changingState ? "Приостанавливаем…" : "Приостановить отклики"}
            </button>
          )}
          {dashboard.system_state === "PAUSED" && (
            <button
              type="button"
              className="primary-button"
              disabled={changingState}
              onClick={() => void controlQueue("resume")}
            >
              <CirclePlay size={19} aria-hidden="true" />
              {changingState ? "Продолжаем…" : "Продолжить отклики"}
            </button>
          )}
          {dashboard.system_state !== "RUNNING" &&
            dashboard.system_state !== "PAUSED" && (
              <button type="button" className="secondary-button" onClick={onRefresh}>
                <RefreshCw size={18} aria-hidden="true" />
                Проверить снова
              </button>
            )}
        </div>
      </section>

      <BackgroundStatusBar dashboard={dashboard} />

      {dashboard.incidents.length > 0 && (
        <section className="incident-list" aria-labelledby="incident-title">
          <div className="section-heading">
            <div>
              <span className="eyebrow">Важно</span>
              <h2 id="incident-title">Ошибки и предупреждения</h2>
            </div>
          </div>
          {dashboard.incidents.map((incident) => (
            <div
              className={`incident-row ${incident.severity.toLowerCase()}`}
              key={incident.id}
            >
              <AlertTriangle size={20} aria-hidden="true" />
              <div>
                <strong>{incident.message}</strong>
                <span>{formatDate(incident.created_at, true)}</span>
              </div>
            </div>
          ))}
        </section>
      )}

      <div className="dashboard-toolbar">
        <h2>Выбранное</h2>
        <button type="button" className="quiet-button" onClick={onOpenPreferences}>
          <SlidersHorizontal size={18} aria-hidden="true" />
          Настроить главную
        </button>
      </div>

      {widgets.length ? (
        <div className="dashboard-grid">
          {(widgets.includes("attention") || widgets.includes("daily")) && (
            <div className="dashboard-column">
              {widgets.includes("attention") && (
                <AttentionWidget forms={forms} onOpen={onOpenAttention} />
              )}
              {widgets.includes("daily") && <DailyWidget dashboard={dashboard} />}
            </div>
          )}
          {(widgets.includes("queue") || widgets.includes("directions")) && (
            <div className="dashboard-column">
              {widgets.includes("queue") && (
                <QueueWidget
                  queue={queue}
                  onOpenVacancy={onOpenVacancy}
                  onOpenAll={onOpenVacancies}
                />
              )}
              {widgets.includes("directions") && <DirectionsWidget dashboard={dashboard} />}
            </div>
          )}
        </div>
      ) : (
        <div className="compact-empty">
          <div>
            <strong>Все дополнительные блоки скрыты</strong>
            <span>Выберите, что показывать на главной.</span>
          </div>
          <button type="button" className="secondary-button" onClick={onOpenPreferences}>
            Настроить главную
          </button>
        </div>
      )}
    </div>
  );
}

function BackgroundStatusBar({ dashboard }: { dashboard: Dashboard }) {
  const background = dashboard.background;
  const presentation = {
    RUNNING: {
      title: "Фоновые проверки работают",
      description: background.next_messages_at
        ? `Сообщения — ${formatDate(background.next_messages_at)}, статусы — ${formatDate(background.next_statuses_at)}`
        : "Расписание создано и ожидает ближайшую проверку",
      tone: "positive",
    },
    NOT_STARTED: {
      title: "Фоновые проверки ещё не запущены",
      description: "Они запустятся вместе с оконной программой Hugin",
      tone: "muted",
    },
    NEEDS_ATTENTION: {
      title: "Фоновым проверкам нужно внимание",
      description: background.error ?? "Одна из проверок остановлена",
      tone: "warning",
    },
    STOPPED: {
      title: "Фоновые проверки не работают",
      description: "Откройте оконную программу Hugin или обновите состояние",
      tone: "danger",
    },
  }[background.state];

  return (
    <section className={`background-status ${presentation.tone}`} aria-label="Фоновые проверки">
      <RefreshCw size={18} aria-hidden="true" />
      <div>
        <strong>{presentation.title}</strong>
        <span>{presentation.description}</span>
      </div>
      {background.next_search_at && background.state === "RUNNING" && (
        <small>
          Следующий поиск — {formatDate(background.next_search_at)}
          {dashboard.resource_saving_mode ? " · бережный режим" : ""}
        </small>
      )}
      {!dashboard.search_enabled && (
        <small>
          Поиск вакансий остановлен
          {dashboard.resource_saving_mode ? " · бережный режим включён" : ""}
        </small>
      )}
    </section>
  );
}

function systemPresentation(state: SystemState, queueLength: number) {
  const queueText = queueLength
    ? `${plural(queueLength, "вакансия", "вакансии", "вакансий")} ждут обработки`
    : "Новых вакансий в очереди нет";
  switch (state) {
    case "RUNNING":
      return {
        title: "Отклики включены",
        description: queueText,
        tone: "positive",
        icon: CheckCircle2,
      };
    case "PAUSED":
      return {
        title: "Отклики приостановлены",
        description: "Новые отклики не отправляются",
        tone: "neutral",
        icon: CirclePause,
      };
    case "AUTH_REQUIRED":
      return {
        title: "Нужно войти на hh.ru",
        description: "После входа вернитесь сюда и проверьте состояние",
        tone: "warning",
        icon: AlertTriangle,
      };
    case "CAPTCHA_REQUIRED":
      return {
        title: "Нужно подтверждение на hh.ru",
        description: "Пройдите проверку в открытом окне браузера",
        tone: "warning",
        icon: AlertTriangle,
      };
    case "ACCOUNT_WARNING":
      return {
        title: "Действия временно ограничены",
        description: "Проверьте предупреждение аккаунта hh.ru",
        tone: "danger",
        icon: AlertTriangle,
      };
  }
}

function AttentionWidget({
  forms,
  onOpen,
}: {
  forms: FormDraft[];
  onOpen: () => void;
}) {
  const inputCount = forms.filter((form) => form.state === "INPUT_REQUIRED").length;
  const reviewCount = forms.filter((form) => form.state === "REVIEW_REQUIRED").length;

  return (
    <section className="dashboard-card attention-card" aria-labelledby="attention-widget-title">
      <div className="card-heading">
        <span className="card-icon amber" aria-hidden="true">
          <Bell size={20} />
        </span>
        <div>
          <h3 id="attention-widget-title">Требует внимания</h3>
          <p>Здесь только действия, которые нельзя выполнить без вас</p>
        </div>
      </div>
      {forms.length ? (
        <div className="attention-summary">
          {inputCount > 0 && (
            <button type="button" className="attention-link" onClick={onOpen}>
              <span>
                <strong>{plural(inputCount, "анкета", "анкеты", "анкет")}</strong>
                <small>нужно заполнить</small>
              </span>
              <ChevronRight size={20} aria-hidden="true" />
            </button>
          )}
          {reviewCount > 0 && (
            <button type="button" className="attention-link" onClick={onOpen}>
              <span>
                <strong>{plural(reviewCount, "анкета", "анкеты", "анкет")}</strong>
                <small>нужно проверить</small>
              </span>
              <ChevronRight size={20} aria-hidden="true" />
            </button>
          )}
        </div>
      ) : (
        <div className="calm-state">
          <CheckCircle2 size={21} aria-hidden="true" />
          <span>Сейчас от вас ничего не требуется</span>
        </div>
      )}
    </section>
  );
}

function QueueWidget({
  queue,
  onOpenVacancy,
  onOpenAll,
}: {
  queue: QueueItem[];
  onOpenVacancy: (vacancyId: string) => Promise<void>;
  onOpenAll: () => void;
}) {
  return (
    <section className="dashboard-card queue-card" aria-labelledby="queue-widget-title">
      <div className="card-heading inline">
        <div className="card-heading-group">
          <span className="card-icon blue" aria-hidden="true">
            <Inbox size={20} />
          </span>
          <div>
            <h3 id="queue-widget-title">Ближайшие вакансии</h3>
            <p>{plural(queue.length, "вакансия", "вакансии", "вакансий")} в очереди</p>
          </div>
        </div>
        {queue.length > 0 && (
          <button type="button" className="text-button" onClick={onOpenAll}>
            Все вакансии
          </button>
        )}
      </div>
      {queue.length ? (
        <ul className="queue-preview">
          {queue.slice(0, 4).map((item) => (
            <li key={item.task_id}>
              <button type="button" onClick={() => void onOpenVacancy(item.vacancy_id)}>
                <span>
                  <strong>{item.title}</strong>
                  <small>
                    {item.company} · {item.region}
                  </small>
                </span>
                <span className="queue-time">{formatDate(item.scheduled_at)}</span>
                <ChevronRight size={19} aria-hidden="true" />
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <div className="calm-state">
          <CheckCircle2 size={21} aria-hidden="true" />
          <span>Очередь пуста</span>
        </div>
      )}
    </section>
  );
}

function DailyWidget({ dashboard }: { dashboard: Dashboard }) {
  const progress =
    dashboard.daily_limit > 0
      ? Math.min((dashboard.applied_today / dashboard.daily_limit) * 100, 100)
      : 0;
  return (
    <section className="dashboard-card daily-card" aria-labelledby="daily-widget-title">
      <div className="card-heading">
        <span className="card-icon green" aria-hidden="true">
          <Gauge size={20} />
        </span>
        <div>
          <h3 id="daily-widget-title">Сегодня</h3>
          <p>Дневное ограничение откликов</p>
        </div>
      </div>
      <div className="daily-value">
        <strong>{dashboard.applied_today}</strong>
        <span>из {dashboard.daily_limit}</span>
      </div>
      <div
        className="progress-track"
        role="progressbar"
        aria-label="Использовано дневное ограничение"
        aria-valuemin={0}
        aria-valuemax={dashboard.daily_limit}
        aria-valuenow={dashboard.applied_today}
      >
        <span style={{ width: `${progress}%` }} />
      </div>
      <p className="card-note">
        Пауза между откликами —{" "}
        {formatDelayRange(dashboard.delay_min_seconds, dashboard.delay_max_seconds)}
      </p>
    </section>
  );
}

function DirectionsWidget({ dashboard }: { dashboard: Dashboard }) {
  const active = dashboard.directions.filter((direction) => direction.is_active);
  return (
    <section className="dashboard-card directions-card" aria-labelledby="directions-widget-title">
      <div className="card-heading">
        <span className="card-icon violet" aria-hidden="true">
          <ListFilter size={20} />
        </span>
        <div>
          <h3 id="directions-widget-title">Направления</h3>
          <p>{plural(active.length, "активное", "активных", "активных")}</p>
        </div>
      </div>
      {active.length ? (
        <ul className="direction-list">
          {active.map((direction) => (
            <li key={direction.id}>
              <span>
                <strong>{direction.name}</strong>
                <small>{plural(direction.queued, "вакансия", "вакансии", "вакансий")} в очереди</small>
              </span>
              <span className="status-pill positive">Включено</span>
            </li>
          ))}
        </ul>
      ) : (
        <div className="calm-state">Активных направлений пока нет</div>
      )}
    </section>
  );
}

function VacanciesView({
  queue,
  sent,
  rejected,
  loading,
  onOpenVacancy,
  onReconcile,
}: {
  queue: QueueItem[];
  sent: SentApplication[];
  rejected: RejectedVacancy[];
  loading: boolean;
  onOpenVacancy: (vacancyId: string) => Promise<void>;
  onReconcile: (taskId: number, status: "APPLIED" | "NOT_FOUND") => Promise<void>;
}) {
  const [tab, setTab] = useState<VacancyTab>("queue");
  const [search, setSearch] = useState("");
  const [reconcilingTask, setReconcilingTask] = useState<number | null>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const queueTabRef = useRef<HTMLButtonElement>(null);
  const sentTabRef = useRef<HTMLButtonElement>(null);
  const rejectedTabRef = useRef<HTMLButtonElement>(null);
  const normalizedSearch = search.trim().toLocaleLowerCase("ru-RU");

  const filteredQueue = useMemo(
    () =>
      queue.filter((item) =>
        `${item.title} ${item.company} ${item.region} ${item.direction}`
          .toLocaleLowerCase("ru-RU")
          .includes(normalizedSearch),
      ),
    [normalizedSearch, queue],
  );
  const filteredRejected = useMemo(
    () =>
      rejected.filter((item) =>
        `${item.title} ${item.company} ${item.region} ${item.direction}`
          .toLocaleLowerCase("ru-RU")
          .includes(normalizedSearch),
      ),
    [normalizedSearch, rejected],
  );
  const filteredSent = useMemo(
    () =>
      sent.filter((item) =>
        `${item.title} ${item.company} ${item.region} ${item.direction} ${item.resume_title}`
          .toLocaleLowerCase("ru-RU")
          .includes(normalizedSearch),
      ),
    [normalizedSearch, sent],
  );

  function onTabKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>): void {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const tabs: VacancyTab[] = ["queue", "sent", "rejected"];
    const currentIndex = tabs.indexOf(tab);
    const nextTab =
      event.key === "Home"
        ? tabs[0]
        : event.key === "End"
          ? tabs[tabs.length - 1]
          : event.key === "ArrowRight"
            ? tabs[(currentIndex + 1) % tabs.length]
            : tabs[(currentIndex - 1 + tabs.length) % tabs.length];
    setTab(nextTab);
    const tabRefs: Record<VacancyTab, RefObject<HTMLButtonElement | null>> = {
      queue: queueTabRef,
      sent: sentTabRef,
      rejected: rejectedTabRef,
    };
    window.requestAnimationFrame(() => tabRefs[nextTab].current?.focus());
  }

  async function reconcile(item: QueueItem, status: "APPLIED" | "NOT_FOUND"): Promise<void> {
    const message =
      status === "APPLIED"
        ? `Подтвердить, что отклик на вакансию «${item.title}» есть в истории hh.ru?`
        : `Подтвердить, что отклика на вакансию «${item.title}» нет в истории hh.ru? Автоматического повтора не будет.`;
    if (!window.confirm(message)) return;
    setReconcilingTask(item.task_id);
    try {
      await onReconcile(item.task_id, status);
    } finally {
      setReconcilingTask(null);
    }
  }

  return (
    <div className="page-stack">
      <div className="list-toolbar">
        <div className="tabs" role="tablist" aria-label="Списки вакансий">
          <button
            ref={queueTabRef}
            id="queue-tab"
            type="button"
            role="tab"
            aria-selected={tab === "queue"}
            aria-controls="queue-panel"
            tabIndex={tab === "queue" ? 0 : -1}
            className={tab === "queue" ? "active" : undefined}
            onClick={() => setTab("queue")}
            onKeyDown={onTabKeyDown}
          >
            В очереди <span>{queue.length}</span>
          </button>
          <button
            ref={sentTabRef}
            id="sent-tab"
            type="button"
            role="tab"
            aria-selected={tab === "sent"}
            aria-controls="sent-panel"
            tabIndex={tab === "sent" ? 0 : -1}
            className={tab === "sent" ? "active" : undefined}
            onClick={() => setTab("sent")}
            onKeyDown={onTabKeyDown}
          >
            Отправлены <span>{sent.length}</span>
          </button>
          <button
            ref={rejectedTabRef}
            id="rejected-tab"
            type="button"
            role="tab"
            aria-selected={tab === "rejected"}
            aria-controls="rejected-panel"
            tabIndex={tab === "rejected" ? 0 : -1}
            className={tab === "rejected" ? "active" : undefined}
            onClick={() => setTab("rejected")}
            onKeyDown={onTabKeyDown}
          >
            Не подошли <span>{rejected.length}</span>
          </button>
        </div>
        <div className="search-field">
          <label className="sr-only" htmlFor="vacancy-search">
            Поиск по вакансиям
          </label>
          <Search size={18} aria-hidden="true" />
          <input
            ref={searchInputRef}
            id="vacancy-search"
            type="search"
            value={search}
            placeholder="Найти вакансию или компанию"
            onChange={(event) => setSearch(event.target.value)}
          />
          {search && (
            <button
              type="button"
              aria-label="Очистить поиск"
              onClick={() => {
                setSearch("");
                searchInputRef.current?.focus();
              }}
            >
              <X size={17} aria-hidden="true" />
            </button>
          )}
        </div>
      </div>

      {tab === "queue" ? (
        <section
          id="queue-panel"
          role="tabpanel"
          aria-labelledby="queue-tab"
          className="vacancy-panel"
        >
          {filteredQueue.length ? (
            <ul className="vacancy-list">
              {filteredQueue.map((item) => (
                <li className="vacancy-row" key={item.task_id}>
                  <div className="vacancy-main">
                    <strong>{item.title}</strong>
                    <span>
                      {item.company} · {item.region}
                    </span>
                    {item.last_error && (
                      <small className="row-error">Ошибка: {item.last_error}</small>
                    )}
                  </div>
                  <div className="vacancy-state">
                    <span className={`status-pill ${stateTone(item.state)}`}>
                      {stateNames[item.state] ?? "Состояние уточняется"}
                    </span>
                    <small>{formatDate(item.scheduled_at)}</small>
                  </div>
                  <div className="vacancy-actions">
                    <button
                      type="button"
                      className="row-action"
                      disabled={loading}
                      onClick={() => void onOpenVacancy(item.vacancy_id)}
                      aria-label={`Открыть вакансию «${item.title}»`}
                    >
                      Открыть
                    </button>
                    {item.state === "UNKNOWN_RESULT" && (
                      <div className="reconciliation-actions">
                        <button
                          type="button"
                          className="secondary-button"
                          disabled={reconcilingTask === item.task_id}
                          onClick={() => void reconcile(item, "APPLIED")}
                        >
                          Есть в истории
                        </button>
                        <button
                          type="button"
                          className="quiet-button"
                          disabled={reconcilingTask === item.task_id}
                          onClick={() => void reconcile(item, "NOT_FOUND")}
                        >
                          Не найден
                        </button>
                      </div>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <SearchEmpty hasSearch={!!normalizedSearch} kind="queue" />
          )}
        </section>
      ) : tab === "sent" ? (
        <section
          id="sent-panel"
          role="tabpanel"
          aria-labelledby="sent-tab"
          className="vacancy-panel"
        >
          {filteredSent.length ? (
            <ul className="vacancy-list">
              {filteredSent.map((item) => (
                <li className="vacancy-row" key={item.application_id}>
                  <div className="vacancy-main">
                    <strong>{item.title}</strong>
                    <span>
                      {item.company} · {item.region}
                    </span>
                    <small>
                      {item.direction} · резюме «{item.resume_title}»
                    </small>
                  </div>
                  <div className="vacancy-state">
                    <span className={`status-pill ${stateTone(item.state)}`}>
                      {stateNames[item.state] ?? "Состояние уточняется"}
                    </span>
                    <small>{formatDate(item.applied_at, true)}</small>
                  </div>
                  <button
                    type="button"
                    className="row-action"
                    disabled={loading}
                    onClick={() => void onOpenVacancy(item.vacancy_id)}
                    aria-label={`Открыть вакансию «${item.title}»`}
                  >
                    Открыть
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <SearchEmpty hasSearch={!!normalizedSearch} kind="sent" />
          )}
        </section>
      ) : (
        <section
          id="rejected-panel"
          role="tabpanel"
          aria-labelledby="rejected-tab"
          className="vacancy-panel"
        >
          {filteredRejected.length ? (
            <ul className="vacancy-list">
              {filteredRejected.map((item) => {
                const reason = visibleReasons(item.reasons)[0];
                return (
                  <li
                    className="vacancy-row rejected"
                    key={`${item.vacancy_id}-${item.direction}`}
                  >
                    <div className="vacancy-main">
                      <strong>{item.title}</strong>
                      <span>
                        {item.company} · {item.region}
                      </span>
                      <small>{reason ?? "Не прошло правила отбора"}</small>
                    </div>
                    <div className="vacancy-state">
                      <span className="status-pill muted">Не подходит</span>
                      {item.score !== null && <small>Оценка {Math.round(item.score)}</small>}
                    </div>
                    <button
                      type="button"
                      className="row-action"
                      disabled={loading}
                      onClick={() => void onOpenVacancy(item.vacancy_id)}
                      aria-label={`Открыть вакансию «${item.title}»`}
                    >
                      Открыть
                    </button>
                  </li>
                );
              })}
            </ul>
          ) : (
            <SearchEmpty hasSearch={!!normalizedSearch} kind="rejected" />
          )}
        </section>
      )}
    </div>
  );
}

function SearchEmpty({
  hasSearch,
  kind,
}: {
  hasSearch: boolean;
  kind: VacancyTab;
}) {
  return (
    <EmptyState
      icon={hasSearch ? <Search size={26} /> : <Inbox size={26} />}
      title={
        hasSearch
          ? "Ничего не найдено"
          : kind === "queue"
            ? "Очередь пуста"
            : kind === "sent"
              ? "Отправленных откликов пока нет"
              : "Отклонённых вакансий нет"
      }
      description={
        hasSearch
          ? "Попробуйте изменить запрос."
          : kind === "queue"
            ? "Новые подходящие вакансии появятся здесь."
            : kind === "sent"
              ? "Подтверждённые отклики и их дальнейшие состояния появятся здесь."
              : "Вакансии, которые не прошли правила отбора, появятся здесь."
      }
    />
  );
}

function AttentionView({
  forms,
  onToast,
}: {
  forms: FormDraft[];
  onToast: (toast: Toast) => void;
}) {
  const [tab, setTab] = useState<AttentionTab>("input");
  const [busyForm, setBusyForm] = useState<number | null>(null);
  const inputTabRef = useRef<HTMLButtonElement>(null);
  const reviewTabRef = useRef<HTMLButtonElement>(null);
  const inputForms = forms.filter((form) => form.state === "INPUT_REQUIRED");
  const reviewForms = forms.filter((form) => form.state === "REVIEW_REQUIRED");
  const visibleForms = tab === "input" ? inputForms : reviewForms;

  function onTabKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>): void {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const nextTab =
      event.key === "Home"
        ? "input"
        : event.key === "End"
          ? "review"
          : tab === "input"
            ? "review"
            : "input";
    setTab(nextTab);
    window.requestAnimationFrame(() =>
      (nextTab === "input" ? inputTabRef : reviewTabRef).current?.focus(),
    );
  }

  async function openForm(form: FormDraft): Promise<void> {
    if (busyForm !== null) return;
    setBusyForm(form.form_id);
    try {
      if (window.pywebview?.api) {
        const result = await window.pywebview.api.open_form(form.vacancy_id);
        if (result.status !== "ok") throw new Error(result.message);
        onToast({ kind: "success", message: result.message });
      } else {
        window.open(form.source_url, "_blank", "noopener,noreferrer");
        onToast({ kind: "success", message: "Анкета открыта в браузере" });
      }
    } catch (reason) {
      onToast({ kind: "error", message: readableError(reason) });
    } finally {
      setBusyForm(null);
    }
  }

  return (
    <div className="page-stack">
      <div className="attention-intro">
        <FileQuestion size={22} aria-hidden="true" />
        <p>
          Помощник уже подготовил доступные ответы. Проверьте только вопросы, где нужна
          ваша информация.
        </p>
      </div>
      <div className="tabs" role="tablist" aria-label="Состояние анкет">
        <button
          ref={inputTabRef}
          id="input-tab"
          type="button"
          role="tab"
          aria-selected={tab === "input"}
          aria-controls="input-panel"
          tabIndex={tab === "input" ? 0 : -1}
          className={tab === "input" ? "active" : undefined}
          onClick={() => setTab("input")}
          onKeyDown={onTabKeyDown}
        >
          Нужно заполнить <span>{inputForms.length}</span>
        </button>
        <button
          ref={reviewTabRef}
          id="review-tab"
          type="button"
          role="tab"
          aria-selected={tab === "review"}
          aria-controls="review-panel"
          tabIndex={tab === "review" ? 0 : -1}
          className={tab === "review" ? "active" : undefined}
          onClick={() => setTab("review")}
          onKeyDown={onTabKeyDown}
        >
          Нужно проверить <span>{reviewForms.length}</span>
        </button>
      </div>
      <section
        id={`${tab}-panel`}
        role="tabpanel"
        aria-labelledby={`${tab}-tab`}
        className="forms-list"
      >
        {visibleForms.length ? (
          visibleForms.map((form) => (
            <article className="form-card" key={form.form_id}>
              <div className="form-header">
                <div>
                  <span className="eyebrow">{form.company}</span>
                  <h2>{form.vacancy_title}</h2>
                  <p>Резюме: {form.resume_title}</p>
                </div>
                <button
                  type="button"
                  className="primary-button"
                  disabled={busyForm !== null}
                  onClick={() => void openForm(form)}
                >
                  <ExternalLink size={18} aria-hidden="true" />
                  {busyForm === form.form_id
                    ? "Открываем…"
                    : tab === "input"
                      ? "Открыть анкету"
                      : "Проверить ответы"}
                </button>
              </div>
              <div className="form-progress">
                <span>{plural(form.answered_count, "ответ", "ответа", "ответов")} готово</span>
                <span>
                  {plural(form.unanswered_count, "вопрос", "вопроса", "вопросов")} без ответа
                </span>
              </div>
              <details className="questions-details">
                <summary>
                  <span>Показать вопросы</span>
                  <ChevronDown size={18} aria-hidden="true" />
                </summary>
                <ol>
                  {form.questions.map((question) => (
                    <li key={question.field_key}>
                      <div>
                        <strong>{question.question}</strong>
                        {question.is_required && <span className="required">Обязательный</span>}
                      </div>
                      <p>{question.answer || "Ответ ещё не указан"}</p>
                      {question.source && (
                        <small>{sourceNames[question.source] ?? question.source}</small>
                      )}
                    </li>
                  ))}
                </ol>
              </details>
            </article>
          ))
        ) : (
          <EmptyState
            icon={<CheckCircle2 size={26} />}
            title={tab === "input" ? "Заполнять ничего не нужно" : "Все ответы проверены"}
            description="Когда потребуется ваше решение, анкета появится здесь."
          />
        )}
      </section>
    </div>
  );
}

function CommunicationsView({
  communications,
  onChanged,
  onToast,
}: {
  communications: Communications;
  onChanged: (communications: Communications) => void;
  onToast: (toast: Toast) => void;
}) {
  const [tab, setTab] = useState<"messages" | "invitations">("messages");
  const [selectedApplicationId, setSelectedApplicationId] = useState<number | null>(
    communications.conversations[0]?.application_id ?? null,
  );
  const [draft, setDraft] = useState("");
  const [replyMode, setReplyMode] = useState<"manual" | "ai">("manual");
  const [busy, setBusy] = useState(false);
  const selected =
    communications.conversations.find(
      (conversation) => conversation.application_id === selectedApplicationId,
    ) ?? communications.conversations[0];
  const reply = selected ? latestEditableReply(selected) : undefined;
  const hasIncoming = selected?.messages.some(
    (message) => message.direction === "INCOMING",
  );

  useEffect(() => {
    if (!selectedApplicationId && communications.conversations[0]) {
      setSelectedApplicationId(communications.conversations[0].application_id);
    }
  }, [communications.conversations, selectedApplicationId]);

  useEffect(() => {
    setDraft(reply?.body ?? "");
    setReplyMode("manual");
  }, [reply?.body, reply?.id, selectedApplicationId]);

  async function selectConversation(conversation: Conversation): Promise<void> {
    setSelectedApplicationId(conversation.application_id);
    if (!conversation.unread_count) return;
    try {
      onChanged(await markConversationRead(conversation.application_id));
    } catch (reason) {
      onToast({ kind: "error", message: readableError(reason) });
    }
  }

  async function saveDraft(): Promise<void> {
    if (!selected || !draft.trim() || busy) return;
    setBusy(true);
    try {
      onChanged(await saveReplyDraft(selected.application_id, draft.trim()));
      onToast({ kind: "success", message: "Черновик ответа сохранён" });
    } catch (reason) {
      onToast({ kind: "error", message: readableError(reason) });
    } finally {
      setBusy(false);
    }
  }

  async function generateDraft(): Promise<void> {
    if (!selected || !hasIncoming || busy) return;
    if (
      draft.trim() &&
      draft.trim() !== reply?.body &&
      !window.confirm("Заменить несохранённый текст новым черновиком?")
    ) {
      return;
    }
    if (!window.pywebview?.api) {
      onToast({
        kind: "error",
        message: "Подготовка ответа доступна только в оконном приложении Hugin",
      });
      return;
    }
    setBusy(true);
    try {
      const result = await window.pywebview.api.generate_reply(
        selected.application_id,
      );
      if (result.status !== "READY") {
        throw new Error(result.message);
      }
      const updated = await loadCommunications();
      onChanged(updated);
      setDraft(result.body ?? "");
      setReplyMode("manual");
      onToast({ kind: "success", message: result.message });
    } catch (reason) {
      onToast({ kind: "error", message: readableError(reason) });
    } finally {
      setBusy(false);
    }
  }

  async function confirmDraft(): Promise<void> {
    if (!reply?.content_hash || busy) return;
    if (
      !window.confirm(
        "Отправить работодателю именно этот текст? После подтверждения Hugin нажмёт кнопку отправки на hh.ru один раз.",
      )
    ) {
      return;
    }
    setBusy(true);
    try {
      if (reply.state !== "CONFIRMED") {
        onChanged(
          await confirmReply(reply.id, reply.content_hash, reply.content_version),
        );
      }
      if (!window.pywebview?.api) {
        throw new Error("Отправка доступна только в оконном приложении Hugin");
      }
      const result = await window.pywebview.api.send_reply(
        reply.id,
        reply.content_hash,
        reply.content_version,
      );
      onChanged(await loadCommunications());
      if (result.status !== "SENT") {
        throw new Error(result.message);
      }
      onToast({ kind: "success", message: result.message });
    } catch (reason) {
      onToast({ kind: "error", message: readableError(reason) });
    } finally {
      setBusy(false);
    }
  }

  async function seeInvitation(invitationId: number): Promise<void> {
    try {
      onChanged(await markInvitationSeen(invitationId));
    } catch (reason) {
      onToast({ kind: "error", message: readableError(reason) });
    }
  }

  async function openCommunicationUrl(url: string): Promise<void> {
    try {
      if (window.pywebview?.api) {
        const result = await window.pywebview.api.open_url(url);
        if (result.status !== "ok" && result.status !== "READY") {
          throw new Error(result.message);
        }
      } else {
        window.open(url, "_blank", "noopener,noreferrer");
      }
    } catch (reason) {
      onToast({ kind: "error", message: readableError(reason) });
    }
  }

  async function openInvitation(
    invitationId: number,
    bookingUrl: string | null,
    sourceUrl: string,
  ): Promise<void> {
    if (!bookingUrl || !window.pywebview?.api) {
      await openCommunicationUrl(bookingUrl ?? sourceUrl);
      return;
    }
    try {
      const result = await window.pywebview.api.open_invitation(invitationId);
      if (result.status !== "ok" && result.status !== "READY") {
        throw new Error(result.message);
      }
    } catch (reason) {
      onToast({ kind: "error", message: readableError(reason) });
    }
  }

  return (
    <div className="page-stack">
      <div className="list-toolbar">
        <div className="tabs" role="tablist" aria-label="Разделы общения">
          <button
            id="messages-tab"
            type="button"
            role="tab"
            aria-selected={tab === "messages"}
            aria-controls="messages-panel"
            className={tab === "messages" ? "active" : undefined}
            onClick={() => setTab("messages")}
          >
            Сообщения <span>{communications.unread_messages}</span>
          </button>
          <button
            id="invitations-tab"
            type="button"
            role="tab"
            aria-selected={tab === "invitations"}
            aria-controls="invitations-panel"
            className={tab === "invitations" ? "active" : undefined}
            onClick={() => setTab("invitations")}
          >
            Приглашения <span>{communications.unseen_invitations}</span>
          </button>
        </div>
        <p className="communications-note">
          Ответ отправляется только после просмотра и явного подтверждения.
        </p>
      </div>

      {tab === "messages" ? (
        <section
          id="messages-panel"
          role="tabpanel"
          aria-labelledby="messages-tab"
          className="communications-layout"
        >
          {communications.conversations.length ? (
            <>
              <ul className="conversation-list" aria-label="Переписки">
                {communications.conversations.map((conversation) => {
                  const latest = conversation.messages.at(-1);
                  return (
                    <li key={conversation.application_id}>
                      <button
                        type="button"
                        className={
                          selected?.application_id === conversation.application_id
                            ? "conversation-button active"
                            : "conversation-button"
                        }
                        onClick={() => void selectConversation(conversation)}
                      >
                        <span className="conversation-heading">
                          <strong>{conversation.company}</strong>
                          {conversation.unread_count > 0 && (
                            <span className="nav-badge">{conversation.unread_count}</span>
                          )}
                        </span>
                        <span>{conversation.vacancy_title}</span>
                        <small>{latest?.body ?? "Сообщений пока нет"}</small>
                      </button>
                    </li>
                  );
                })}
              </ul>
              {selected && (
                <article className="conversation-panel" aria-label="Переписка">
                  <header>
                    <div>
                      <span className="eyebrow">{selected.company}</span>
                      <h2>{selected.vacancy_title}</h2>
                    </div>
                    <div className="conversation-header-actions">
                      {selected.needs_reply && (
                        <span className="status-pill warning">Нужен ответ</span>
                      )}
                      <button
                        type="button"
                        className="secondary-button"
                        onClick={() => void openCommunicationUrl(selected.source_url)}
                      >
                        <ExternalLink size={16} aria-hidden="true" />
                        Открыть на hh.ru
                      </button>
                    </div>
                  </header>
                  <ol className="message-thread">
                    {selected.messages.map((message) => (
                      <li
                        className={
                          message.direction === "INCOMING"
                            ? "message-bubble incoming"
                            : "message-bubble outgoing"
                        }
                        key={message.id}
                      >
                        <span>
                          {message.direction === "INCOMING" ? "Работодатель" : "Вы"}
                        </span>
                        <p>{message.body}</p>
                        <small>
                          {formatDate(message.occurred_at, true)}
                          {message.direction === "OUTGOING"
                            ? ` · ${stateNames[message.state] ?? message.state}`
                            : ""}
                        </small>
                      </li>
                    ))}
                  </ol>
                  <div className="reply-editor">
                    <div className="reply-mode" role="group" aria-label="Способ ответа">
                      <button
                        type="button"
                        className={replyMode === "manual" ? "active" : undefined}
                        aria-pressed={replyMode === "manual"}
                        onClick={() => setReplyMode("manual")}
                      >
                        Написать самому
                      </button>
                      <button
                        type="button"
                        className={replyMode === "ai" ? "active" : undefined}
                        aria-pressed={replyMode === "ai"}
                        onClick={() => setReplyMode("ai")}
                      >
                        <Sparkles size={15} aria-hidden="true" />
                        Сгенерировать нейросетью
                      </button>
                    </div>
                    {replyMode === "manual" ? (
                      <>
                        <label htmlFor="reply-draft">Черновик ответа</label>
                        <textarea
                          id="reply-draft"
                          value={draft}
                          rows={6}
                          maxLength={5000}
                          placeholder="Введите ответ работодателю"
                          onChange={(event) => setDraft(event.target.value)}
                        />
                        <div className="reply-actions">
                          <span>
                            {reply?.state === "CONFIRMED"
                              ? "Текст подтверждён и готов к отправке."
                              : "Сохраните текст и проверьте его перед отправкой."}
                          </span>
                          <button
                            type="button"
                            className="secondary-button"
                            disabled={
                              busy || !draft.trim() || draft.trim() === reply?.body
                            }
                            onClick={() => void saveDraft()}
                          >
                            Сохранить
                          </button>
                          <button
                            type="button"
                            className="primary-button"
                            disabled={
                              busy ||
                              !reply?.content_hash ||
                              draft.trim() !== reply.body
                            }
                            onClick={() => void confirmDraft()}
                          >
                            {reply?.state === "CONFIRMED"
                              ? "Отправить подтверждённый"
                              : "Подтвердить и отправить"}
                          </button>
                        </div>
                      </>
                    ) : (
                      <div className="reply-generation">
                        <div>
                          <strong>Подготовить черновик по переписке</strong>
                          <span>
                            {hasIncoming
                              ? "Нейросеть учтёт вакансию и только разрешённые вами подтверждённые сведения. Перед отправкой текст можно изменить."
                              : "Черновик можно будет подготовить после первого сообщения работодателя."}
                          </span>
                        </div>
                        <button
                          type="button"
                          className="primary-button"
                          disabled={busy || !hasIncoming}
                          onClick={() => void generateDraft()}
                        >
                          <Sparkles size={16} aria-hidden="true" />
                          {busy
                            ? "Готовим…"
                            : hasIncoming
                              ? "Подготовить черновик"
                              : "Пока нечего отвечать"}
                        </button>
                      </div>
                    )}
                  </div>
                </article>
              )}
            </>
          ) : (
            <EmptyState
              icon={<MessageSquare size={26} />}
              title="Сообщений пока нет"
              description="Новые сообщения работодателей появятся здесь."
            />
          )}
        </section>
      ) : (
        <section
          id="invitations-panel"
          role="tabpanel"
          aria-labelledby="invitations-tab"
          className="invitation-list"
        >
          {communications.invitations.length ? (
            communications.invitations.map((invitation) => (
              <article
                className={invitation.seen_at ? "invitation-card" : "invitation-card unseen"}
                key={invitation.id}
              >
                <div>
                  <span className="eyebrow">{invitation.company}</span>
                  <h2>{invitation.title}</h2>
                  <p>{invitation.vacancy_title}</p>
                  {invitation.details && <div>{invitation.details}</div>}
                  {invitation.interview_at && (
                    <strong>
                      Встреча: {formatDate(invitation.interview_at, true)}
                    </strong>
                  )}
                </div>
                <div className="invitation-actions">
                  {!invitation.seen_at && (
                    <button
                      type="button"
                      className="secondary-button"
                      onClick={() => void seeInvitation(invitation.id)}
                    >
                      Отметить просмотренным
                    </button>
                  )}
                  <span className={`status-pill ${stateTone(invitation.state)}`}>
                    {stateNames[invitation.state] ?? "Получено"}
                  </span>
                  <button
                    type="button"
                    className="secondary-button"
                    onClick={() =>
                      void openInvitation(
                        invitation.id,
                        invitation.booking_url,
                        invitation.source_url,
                      )
                    }
                  >
                    <ExternalLink size={16} aria-hidden="true" />
                    {invitation.booking_url ? "Открыть запись" : "Открыть на hh.ru"}
                  </button>
                </div>
              </article>
            ))
          ) : (
            <EmptyState
              icon={<Bell size={26} />}
              title="Приглашений пока нет"
              description="Приглашения на звонок или собеседование появятся здесь."
            />
          )}
        </section>
      )}
    </div>
  );
}

function latestEditableReply(conversation: Conversation) {
  return [...conversation.messages]
    .reverse()
    .find(
      (message) =>
        message.direction === "OUTGOING" &&
        ["DRAFT", "REVIEW_REQUIRED", "CONFIRMED", "FAILED"].includes(message.state),
    );
}

const profileCategoryNames: Record<string, string> = {
  full_name: "Имя",
  desired_position: "Желаемая должность",
  location: "Место проживания",
  citizenship: "Гражданство",
  employment: "Занятость",
  work_format: "Формат работы",
  mobility: "Переезд",
  email: "Электронная почта",
  phone: "Телефон",
  telegram: "Telegram",
  github: "GitHub",
  work_experience: "Опыт работы",
  education: "Образование",
  courses: "Курсы",
  skills: "Навыки",
  about: "О себе",
  languages: "Языки",
  salary_expectation: "Зарплата",
  available_from: "Дата выхода",
  work_schedule: "График",
  relocation: "Переезд",
  business_trips: "Командировки",
  english_level: "Английский язык",
  work_authorization: "Разрешение на работу",
  portfolio: "Портфолио",
  job_search_reason: "Причина поиска",
  test_assignment: "Проверочное задание",
};

function profileCategoryName(category: string): string {
  return profileCategoryNames[category] ?? category.replaceAll("_", " ");
}

function formatFileSize(value: number | null): string {
  if (value === null) return "Размер не указан";
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} КБ`;
  return `${(value / (1024 * 1024)).toFixed(1).replace(".", ",")} МБ`;
}

function ProfileView({
  profile,
  onProfileChanged,
  onToast,
}: {
  profile: Profile;
  onProfileChanged: (profile: Profile) => void;
  onToast: (toast: Toast) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [preview, setPreview] = useState<ResumePreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pendingFacts = profile.facts.filter((fact) => fact.state === "PENDING");
  const confirmedFacts = profile.facts.filter((fact) => fact.state === "CONFIRMED");
  const rejectedFacts = profile.facts.filter((fact) => fact.state === "REJECTED");
  const pendingQuestions = profile.questions.filter((question) => question.state === "PENDING");
  const dismissedQuestions = profile.questions.filter(
    (question) => question.state === "DISMISSED",
  );

  async function chooseResume(file: File | undefined): Promise<void> {
    if (!file || previewing) return;
    setPreviewing(true);
    setError(null);
    setPreview(null);
    try {
      setPreview(await previewResume(file));
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setPreviewing(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  async function confirmImport(): Promise<void> {
    if (!preview || importing) return;
    setImporting(true);
    setError(null);
    try {
      const saved = await importResume(preview.token);
      onProfileChanged(saved);
      setPreview(null);
      onToast({ kind: "success", message: "Резюме импортировано и ожидает проверки фактов" });
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setImporting(false);
    }
  }

  return (
    <div className="profile-layout">
      <section className="profile-card resume-card" aria-labelledby="active-resume-title">
        <div className="section-heading">
          <div>
            <span className="eyebrow">Основные данные</span>
            <h2 id="active-resume-title">Активное резюме</h2>
            <p>Исходный файл хранится только на этом компьютере.</p>
          </div>
          <button
            type="button"
            className="primary-button"
            disabled={previewing || importing}
            onClick={() => inputRef.current?.click()}
          >
            <Upload size={18} aria-hidden="true" />
            {previewing
              ? "Проверяем…"
              : profile.active_resume
                ? "Заменить файл"
                : "Выбрать файл"}
          </button>
          <input
            ref={inputRef}
            className="visually-hidden"
            type="file"
            accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            onChange={(event) => void chooseResume(event.target.files?.[0])}
          />
        </div>

        {profile.active_resume ? (
          <div className="resume-summary">
            <span className="resume-file-icon" aria-hidden="true">
              {profile.active_resume.source_type ?? "CV"}
            </span>
            <div>
              <strong>{profile.active_resume.title}</strong>
              <span>
                {profile.active_resume.source_original_name ?? "Файл ещё не импортирован"}
              </span>
              <small>
                {formatFileSize(profile.active_resume.source_size_bytes)}
                {profile.active_resume.source_page_count
                  ? ` · ${plural(profile.active_resume.source_page_count, "страница", "страницы", "страниц")}`
                  : ""}
                {profile.active_resume.imported_at
                  ? ` · импортировано ${formatDate(profile.active_resume.imported_at, true)}`
                  : ""}
              </small>
            </div>
          </div>
        ) : (
          <EmptyState
            icon={<UserRound size={26} />}
            title="Резюме ещё не импортировано"
            description="Выберите PDF или DOCX. Сначала программа покажет, что именно она нашла."
          />
        )}

        {preview && (
          <div className="resume-preview" role="region" aria-label="Проверка нового резюме">
            <div className="resume-preview-heading">
              <div>
                <span className="eyebrow">Предварительная проверка</span>
                <h3>{preview.title}</h3>
                <p>
                  {preview.original_name} · {preview.source_type}
                  {preview.page_count
                    ? ` · ${plural(preview.page_count, "страница", "страницы", "страниц")}`
                    : ""}
                </p>
              </div>
              <span className="status-pill positive">Файл читается</span>
            </div>
            <div className="preview-counts">
              <span>{plural(preview.facts.length, "сведение", "сведения", "сведений")}</span>
              <span>
                {plural(preview.questions.length, "вопрос", "вопроса", "вопросов")} без ответа
              </span>
            </div>
            <details className="preview-details">
              <summary>
                <span>Посмотреть найденные сведения</span>
                <ChevronDown size={18} aria-hidden="true" />
              </summary>
              <ul>
                {preview.facts.map((fact, index) => (
                  <li key={`${fact.category}-${index}`}>
                    <strong>{profileCategoryName(fact.category)}</strong>
                    <span>{fact.content}</span>
                  </li>
                ))}
              </ul>
            </details>
            {profile.active_resume && (
              <p className="settings-warning">
                <AlertTriangle size={18} aria-hidden="true" />
                Ранее подтверждённые сведения сохранятся. После импорта проверьте список и
                отклоните устаревшие данные вручную.
              </p>
            )}
            <div className="settings-form-actions">
              <button
                type="button"
                className="primary-button"
                disabled={importing}
                onClick={() => void confirmImport()}
              >
                {importing ? "Импортируем…" : "Импортировать и сделать активным"}
              </button>
              <button
                type="button"
                className="secondary-button"
                disabled={importing}
                onClick={() => setPreview(null)}
              >
                Отменить
              </button>
            </div>
          </div>
        )}
        {error && (
          <p className="settings-submit-error" role="alert">
            {error}
          </p>
        )}
      </section>

      <section className="profile-card wide" aria-labelledby="facts-title">
        <div className="section-heading">
          <div>
            <span className="eyebrow">Проверка</span>
            <h2 id="facts-title">Сведения из резюме</h2>
            <p>Непроверенные сведения не используются в письмах, анкетах и сообщениях.</p>
          </div>
          <span className={pendingFacts.length ? "count-badge warning" : "count-badge"}>
            {plural(pendingFacts.length, "ожидает", "ожидают", "ожидают")}
          </span>
        </div>
        {pendingFacts.length ? (
          <div className="profile-fact-list">
            {pendingFacts.map((fact) => (
              <ProfileFactReview
                key={fact.id}
                fact={fact}
                onProfileChanged={onProfileChanged}
                onToast={onToast}
              />
            ))}
          </div>
        ) : (
          <div className="calm-state">Все новые сведения проверены</div>
        )}
        {(confirmedFacts.length > 0 || rejectedFacts.length > 0) && (
          <details className="profile-history-details">
            <summary>
              <span>
                Проверено: {confirmedFacts.length} · отклонено: {rejectedFacts.length}
              </span>
              <ChevronDown size={18} aria-hidden="true" />
            </summary>
            <div className="profile-reviewed-grid">
              {[...confirmedFacts, ...rejectedFacts].map((fact) => (
                <article key={fact.id}>
                  <div>
                    <strong>{profileCategoryName(fact.category)}</strong>
                    <span
                      className={`status-pill ${
                        fact.state === "CONFIRMED" ? "positive" : "muted"
                      }`}
                    >
                      {fact.state === "CONFIRMED" ? "Подтверждено" : "Отклонено"}
                    </span>
                  </div>
                  <p>{fact.content}</p>
                </article>
              ))}
            </div>
          </details>
        )}
      </section>

      <section className="profile-card wide" aria-labelledby="answers-title">
        <div className="section-heading">
          <div>
            <span className="eyebrow">Частые вопросы</span>
            <h2 id="answers-title">Сохранённые ответы</h2>
            <p>Ответ применяется только после вашего подтверждения.</p>
          </div>
          <span className={pendingQuestions.length ? "count-badge warning" : "count-badge"}>
            {plural(pendingQuestions.length, "без ответа", "без ответа", "без ответа")}
          </span>
        </div>
        {pendingQuestions.length > 0 && (
          <div className="profile-question-list">
            {pendingQuestions.map((question) => (
              <ProfileQuestionEditor
                key={question.key}
                question={question}
                onProfileChanged={onProfileChanged}
                onToast={onToast}
              />
            ))}
          </div>
        )}
        {profile.answers.length > 0 && (
          <div className="answer-bank">
            {profile.answers.map((answer) => (
              <article key={answer.key}>
                <strong>{answer.question}</strong>
                <p>{answer.answer}</p>
              </article>
            ))}
          </div>
        )}
        {!pendingQuestions.length && !profile.answers.length && (
          <div className="calm-state">Частых вопросов пока нет</div>
        )}
        {dismissedQuestions.length > 0 && (
          <p className="settings-note">
            Отложено без ответа: {dismissedQuestions.length}. Они не ограничивают поиск и не
            подставляются в анкеты.
          </p>
        )}
      </section>
    </div>
  );
}

function ProfileFactReview({
  fact,
  onProfileChanged,
  onToast,
}: {
  fact: ProfileFact;
  onProfileChanged: (profile: Profile) => void;
  onToast: (toast: Toast) => void;
}) {
  const [allowLetters, setAllowLetters] = useState(false);
  const [allowForms, setAllowForms] = useState(false);
  const [allowMessages, setAllowMessages] = useState(false);
  const [busy, setBusy] = useState<"confirm" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function review(action: "confirm" | "reject"): Promise<void> {
    if (busy) return;
    setBusy(action);
    setError(null);
    try {
      const saved = await reviewProfileFact(
        fact.id,
        action,
        action === "confirm"
          ? {
              allow_in_letters: allowLetters,
              allow_in_forms: allowForms,
              allow_in_messages: allowMessages,
            }
          : undefined,
      );
      onProfileChanged(saved);
      onToast({
        kind: "success",
        message: action === "confirm" ? "Сведение подтверждено" : "Сведение отклонено",
      });
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setBusy(null);
    }
  }

  return (
    <article className="profile-fact-card">
      <div className="profile-fact-heading">
        <span className="eyebrow">{profileCategoryName(fact.category)}</span>
        <span className="status-pill warning">Нужно проверить</span>
      </div>
      <p>{fact.content}</p>
      <fieldset>
        <legend>Где можно использовать после подтверждения</legend>
        <div>
          <label>
            <input
              type="checkbox"
              checked={allowLetters}
              onChange={(event) => setAllowLetters(event.target.checked)}
            />
            <span>В письмах</span>
          </label>
          <label>
            <input
              type="checkbox"
              checked={allowForms}
              onChange={(event) => setAllowForms(event.target.checked)}
            />
            <span>В анкетах</span>
          </label>
          <label>
            <input
              type="checkbox"
              checked={allowMessages}
              onChange={(event) => setAllowMessages(event.target.checked)}
            />
            <span>В сообщениях</span>
          </label>
        </div>
      </fieldset>
      {error && (
        <p className="settings-submit-error" role="alert">
          {error}
        </p>
      )}
      <div className="profile-review-actions">
        <button
          type="button"
          className="primary-button"
          disabled={busy !== null}
          onClick={() => void review("confirm")}
        >
          {busy === "confirm" ? "Сохраняем…" : "Подтвердить"}
        </button>
        <button
          type="button"
          className="secondary-button"
          disabled={busy !== null}
          onClick={() => void review("reject")}
        >
          {busy === "reject" ? "Отклоняем…" : "Отклонить"}
        </button>
      </div>
    </article>
  );
}

function ProfileQuestionEditor({
  question,
  onProfileChanged,
  onToast,
}: {
  question: ProfileQuestion;
  onProfileChanged: (profile: Profile) => void;
  onToast: (toast: Toast) => void;
}) {
  const [answer, setAnswer] = useState(question.answer ?? "");
  const [busy, setBusy] = useState<"save" | "dismiss" | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function save(): Promise<void> {
    if (busy) return;
    if (!answer.trim()) {
      setError("Введите ответ или отложите вопрос");
      return;
    }
    setBusy("save");
    setError(null);
    try {
      onProfileChanged(await saveProfileAnswer(question.key, answer));
      onToast({ kind: "success", message: "Ответ сохранён" });
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setBusy(null);
    }
  }

  async function dismiss(): Promise<void> {
    if (busy) return;
    setBusy("dismiss");
    setError(null);
    try {
      onProfileChanged(await dismissProfileQuestion(question.key));
      onToast({ kind: "success", message: "Вопрос отложен" });
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setBusy(null);
    }
  }

  return (
    <article className="profile-question-card">
      <label>
        <strong>{question.question}</strong>
        <textarea
          rows={3}
          value={answer}
          placeholder="Введите подтверждённый ответ"
          onChange={(event) => setAnswer(event.target.value)}
        />
      </label>
      {error && (
        <p className="settings-submit-error" role="alert">
          {error}
        </p>
      )}
      <div className="profile-review-actions">
        <button
          type="button"
          className="primary-button"
          disabled={busy !== null}
          onClick={() => void save()}
        >
          {busy === "save" ? "Сохраняем…" : "Сохранить ответ"}
        </button>
        <button
          type="button"
          className="secondary-button"
          disabled={busy !== null}
          onClick={() => void dismiss()}
        >
          {busy === "dismiss" ? "Откладываем…" : "Отложить"}
        </button>
      </div>
    </article>
  );
}

function SettingsView({
  dashboard,
  directionOptions,
  notificationSettings,
  aiModelSettings,
  aiPromptSettings,
  widgets,
  onToggleWidget,
  onResetWidgets,
  onSettingsSaved,
  onDirectionSaved,
  onRefresh,
  onNotificationsSaved,
  onToast,
}: {
  dashboard: Dashboard;
  directionOptions: DirectionOptions;
  notificationSettings: NotificationSettings;
  aiModelSettings: AiModelSettings;
  aiPromptSettings: AiPromptSettings;
  widgets: DashboardWidget[];
  onToggleWidget: (widget: DashboardWidget) => void;
  onResetWidgets: () => void;
  onSettingsSaved: (settings: QueueSettings) => void;
  onDirectionSaved: (direction: DirectionSummary) => void;
  onRefresh: () => void;
  onNotificationsSaved: (communications: Communications) => void;
  onToast: (toast: Toast) => void;
}) {
  return (
    <div className="settings-layout">
      <section className="settings-card" aria-labelledby="home-settings-title">
        <div className="section-heading">
          <div>
            <span className="eyebrow">Главная</span>
            <h2 id="home-settings-title">Что показывать</h2>
            <p>Состояние программы и важные предупреждения видны всегда.</p>
          </div>
          <button type="button" className="quiet-button" onClick={onResetWidgets}>
            <RotateCcw size={17} aria-hidden="true" />
            Вернуть стандартный вид
          </button>
        </div>
        <WidgetSelector widgets={widgets} onToggle={onToggleWidget} />
      </section>

      <section className="settings-card" aria-labelledby="rules-title">
        <div className="section-heading">
          <div>
            <span className="eyebrow">Работа очереди</span>
            <h2 id="rules-title">Отклики</h2>
            <p>Эти параметры применяются ко всем новым откликам.</p>
          </div>
        </div>
        <QueueSettingsForm
          dashboard={dashboard}
          onSaved={onSettingsSaved}
          onToast={onToast}
        />
      </section>

      <section className="settings-card wide" aria-labelledby="directions-title">
        <div className="section-heading">
          <div>
            <span className="eyebrow">Поиск</span>
            <h2 id="directions-title">Направления</h2>
            <p>По каким направлениям сейчас подбираются вакансии.</p>
          </div>
        </div>
        <div className="settings-directions">
          {dashboard.directions.map((direction) => (
            <DirectionSettingsCard
              key={direction.id}
              direction={direction}
              availableRegions={directionOptions.regions}
              onSaved={onDirectionSaved}
              onToast={onToast}
            />
          ))}
        </div>
      </section>

      <section className="settings-card" aria-labelledby="background-mode-title">
        <div className="section-heading">
          <div>
            <span className="eyebrow">Фоновая работа</span>
            <h2 id="background-mode-title">Нагрузка на компьютер</h2>
            <p>Бережный режим работает медленнее, но реже запускает проверки hh.ru.</p>
          </div>
        </div>
        <ResourceSavingModeControl
          enabled={dashboard.resource_saving_mode}
          onSaved={onRefresh}
          onToast={onToast}
        />
      </section>

      <section className="settings-card wide" aria-labelledby="ai-prompts-title">
        <div className="section-heading">
          <div>
            <span className="eyebrow">Нейросеть</span>
            <h2 id="ai-prompts-title">Модель и инструкции</h2>
            <p>Выберите модель для новых текстов. Дополнительные инструкции можно изменить ниже.</p>
          </div>
        </div>
        <AiModelSelector
          settings={aiModelSettings}
          onSaved={onNotificationsSaved}
          onToast={onToast}
        />
        <AiPromptSettingsForm
          settings={aiPromptSettings}
          onSaved={onNotificationsSaved}
          onToast={onToast}
        />
      </section>

      <section className="settings-card wide" aria-labelledby="notifications-title">
        <div className="section-heading">
          <div>
            <span className="eyebrow">Важные события</span>
            <h2 id="notifications-title">Уведомления</h2>
            <p>Выберите, о чём программа должна сообщать на этом компьютере.</p>
          </div>
        </div>
        <NotificationSettingsForm
          settings={notificationSettings}
          onSaved={onNotificationsSaved}
          onToast={onToast}
        />
      </section>
    </div>
  );
}

function ResourceSavingModeControl({
  enabled,
  onSaved,
  onToast,
}: {
  enabled: boolean;
  onSaved: () => void;
  onToast: (toast: Toast) => void;
}) {
  const [busy, setBusy] = useState(false);

  async function change(next: boolean): Promise<void> {
    if (busy) return;
    setBusy(true);
    try {
      await updateResourceSavingMode(next);
      onSaved();
      onToast({
        kind: "success",
        message: next ? "Бережный режим включён" : "Обычный режим включён",
      });
    } catch (reason) {
      onToast({ kind: "error", message: readableError(reason) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="resource-saving-control">
      <label className="direction-active-control">
        <span className="check-control">
          <input
            type="checkbox"
            checked={enabled}
            disabled={busy}
            onChange={(event) => void change(event.target.checked)}
          />
          <span aria-hidden="true" />
        </span>
        <span>
          <strong>Бережный режим</strong>
          <small>
            Поиск — не чаще раза в 4 часа, сообщения — раз в 15 минут, статусы — раз в час.
          </small>
        </span>
      </label>
      <p>
        Браузер остаётся обычным и доступным пользователю, но при фоновой проверке
        открывается свёрнутым. Если hh.ru попросит вход или дополнительную проверку,
        окно можно развернуть вручную.
      </p>
    </div>
  );
}

function AiModelSelector({
  settings,
  onSaved,
  onToast,
}: {
  settings: AiModelSettings;
  onSaved: (communications: Communications) => void;
  onToast: (toast: Toast) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save(model: string, reasoningEffort: string, message: string): Promise<void> {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const communications = await updateAiModelSettings(model, reasoningEffort);
      onSaved(communications);
      onToast({ kind: "success", message });
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <fieldset className="ai-model-selector" disabled={busy}>
      <legend>Модель для всех новых текстов</legend>
      <div className="ai-model-options">
        {settings.options.map((option) => (
          <label
            className={option.value === settings.selected ? "selected" : ""}
            key={option.value}
          >
            <input
              type="radio"
              name="ai-model"
              value={option.value}
              checked={option.value === settings.selected}
              onChange={() =>
                void save(
                  option.value,
                  settings.reasoning_effort,
                  `Выбрана модель ${option.title}`,
                )
              }
            />
            <span>
              <strong>{option.title}</strong>
              <small>{option.description}</small>
            </span>
          </label>
        ))}
      </div>
      <label className="ai-reasoning-select">
        <span>
          <strong>Глубина обработки</strong>
          <small>Глубокий режим выбран по умолчанию для более тщательных текстов.</small>
        </span>
        <select
          value={settings.reasoning_effort}
          onChange={(event) => {
            const option = settings.reasoning_options.find(
              (item) => item.value === event.target.value,
            );
            if (option) {
              void save(
                settings.selected,
                option.value,
                `Выбран режим «${option.title}»`,
              );
            }
          }}
        >
          {settings.reasoning_options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.title} — {option.description}
            </option>
          ))}
        </select>
      </label>
      <small className="ai-model-note">
        Выбор применяется к резюме, сопроводительным письмам и черновикам ответов. Готовые тексты
        не изменяются. Для Qwen выбранный режим является приоритетом, окончательную глубину
        модель определяет сама.
      </small>
      {error && (
        <p className="settings-submit-error" role="alert">
          {error}
        </p>
      )}
    </fieldset>
  );
}

function AiPromptSettingsForm({
  settings,
  onSaved,
  onToast,
}: {
  settings: AiPromptSettings;
  onSaved: (communications: Communications) => void;
  onToast: (toast: Toast) => void;
}) {
  const [values, setValues] = useState<AiPromptValues>({
    resume: settings.resume,
    cover_letter: settings.cover_letter,
    recruiter_reply: settings.recruiter_reply,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setValues({
      resume: settings.resume,
      cover_letter: settings.cover_letter,
      recruiter_reply: settings.recruiter_reply,
    });
  }, [settings.cover_letter, settings.recruiter_reply, settings.resume]);

  const dirty =
    values.resume.trim() !== settings.resume ||
    values.cover_letter.trim() !== settings.cover_letter ||
    values.recruiter_reply.trim() !== settings.recruiter_reply;
  const complete =
    Boolean(values.resume.trim()) &&
    Boolean(values.cover_letter.trim()) &&
    Boolean(values.recruiter_reply.trim());

  function change(key: keyof AiPromptValues, value: string): void {
    setValues((current) => ({ ...current, [key]: value }));
    setError(null);
  }

  async function save(): Promise<void> {
    if (!dirty || !complete || busy) return;
    setBusy(true);
    setError(null);
    try {
      const communications = await updateAiPromptSettings(values);
      onSaved(communications);
      onToast({ kind: "success", message: "Инструкции нейросети сохранены" });
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setBusy(false);
    }
  }

  async function reset(): Promise<void> {
    if (busy || !window.confirm("Вернуть стандартные инструкции нейросети?")) return;
    setBusy(true);
    setError(null);
    try {
      const communications = await resetAiPromptSettings();
      onSaved(communications);
      onToast({ kind: "success", message: "Стандартные инструкции восстановлены" });
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <details className="ai-prompt-settings">
      <summary>
        <span>
          <strong>Изменить инструкции</strong>
          <small>Для резюме, сопроводительных писем и ответов работодателям</small>
        </span>
        <ChevronDown size={18} aria-hidden="true" />
      </summary>
      <div className="ai-prompt-fields">
        {[
          {
            key: "resume" as const,
            title: "Улучшение резюме",
            hint: "Как переписывать блоки резюме под выбранную роль.",
          },
          {
            key: "cover_letter" as const,
            title: "Сопроводительные письма",
            hint: "Стиль и подача письма для конкретной вакансии.",
          },
          {
            key: "recruiter_reply" as const,
            title: "Ответы работодателям",
            hint: "Как готовить черновики по текущей переписке.",
          },
        ].map((field) => (
          <label className="text-field" key={field.key}>
            <span>{field.title}</span>
            <small>{field.hint}</small>
            <textarea
              rows={4}
              maxLength={4000}
              value={values[field.key]}
              onChange={(event) => change(field.key, event.target.value)}
            />
          </label>
        ))}
      </div>
      <p className="settings-note">
        Правила точности и запрет отправки без подтверждения изменить нельзя.
      </p>
      {error && (
        <p className="settings-submit-error" role="alert">
          {error}
        </p>
      )}
      <div className="settings-form-actions">
        <button
          type="button"
          className="primary-button"
          disabled={!dirty || !complete || busy}
          onClick={() => void save()}
        >
          {busy ? "Сохраняем…" : "Сохранить"}
        </button>
        <button
          type="button"
          className="secondary-button"
          disabled={busy}
          onClick={() => void reset()}
        >
          Вернуть стандартные
        </button>
      </div>
    </details>
  );
}

const notificationEventOptions = [
  {
    id: "NEW_MESSAGE",
    title: "Новое сообщение",
    description: "Работодатель написал по отклику.",
  },
  {
    id: "INVITATION",
    title: "Приглашение",
    description: "Появилось приглашение на звонок или собеседование.",
  },
  {
    id: "REPLY_REQUIRED",
    title: "Нужен ответ",
    description: "Переписка ждёт вашего решения.",
  },
  {
    id: "FORM_REQUIRED",
    title: "Нужно заполнить анкету",
    description: "Без ваших данных отклик нельзя продолжить.",
  },
  {
    id: "AUTH_REQUIRED",
    title: "Нужно войти в hh.ru",
    description: "Поиск остановлен до входа или проверки.",
  },
  {
    id: "ACCOUNT_WARNING",
    title: "Предупреждение аккаунта",
    description: "hh.ru ограничил действие или показал важное предупреждение.",
  },
  {
    id: "UNKNOWN_RESULT",
    title: "Результат не подтверждён",
    description: "Нужно сверить действие с историей на hh.ru.",
  },
  {
    id: "CRITICAL_ERROR",
    title: "Критическая ошибка",
    description: "Программа не может безопасно продолжить работу.",
  },
  {
    id: "DAILY_SUMMARY",
    title: "Итоги дня",
    description: "Краткая сводка по поиску и откликам.",
  },
] as const;

function selectedNotificationEvents(settings: NotificationSettings): string[] {
  return notificationEventOptions
    .filter((event) => (settings.routing[event.id]?.length ?? 0) > 0)
    .map((event) => event.id);
}

function NotificationSettingsForm({
  settings,
  onSaved,
  onToast,
}: {
  settings: NotificationSettings;
  onSaved: (communications: Communications) => void;
  onToast: (toast: Toast) => void;
}) {
  const initialEvents = selectedNotificationEvents(settings);
  const [baselineChannels, setBaselineChannels] = useState([
    settings.windows_enabled,
    settings.telegram_enabled,
    settings.email_enabled,
  ]);
  const [baselineEvents, setBaselineEvents] = useState(initialEvents);
  const [windowsEnabled, setWindowsEnabled] = useState(settings.windows_enabled);
  const [telegramEnabled, setTelegramEnabled] = useState(settings.telegram_enabled);
  const [emailEnabled, setEmailEnabled] = useState(settings.email_enabled);
  const [events, setEvents] = useState(initialEvents);
  const [saving, setSaving] = useState(false);
  const [serviceAvailable, setServiceAvailable] = useState(false);
  const [gatewayKeyConfigured, setGatewayKeyConfigured] = useState(false);
  const [telegramAvailable, setTelegramAvailable] = useState(false);
  const [telegramConnected, setTelegramConnected] = useState(false);
  const [telegramBotUsername, setTelegramBotUsername] = useState("hugin_workbot");
  const [serviceStatusLoading, setServiceStatusLoading] = useState(true);
  const [emailConfigured, setEmailConfigured] = useState(false);
  const [credentialsSaving, setCredentialsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dirty =
    [windowsEnabled, telegramEnabled, emailEnabled].join("|") !==
      baselineChannels.join("|") ||
    events.join("|") !== baselineEvents.join("|");
  const anyChannel = windowsEnabled || telegramEnabled || emailEnabled;

  useEffect(() => {
    if (dirty || saving) return;
    const incoming = selectedNotificationEvents(settings);
    setBaselineChannels([
      settings.windows_enabled,
      settings.telegram_enabled,
      settings.email_enabled,
    ]);
    setBaselineEvents(incoming);
    setWindowsEnabled(settings.windows_enabled);
    setTelegramEnabled(settings.telegram_enabled);
    setEmailEnabled(settings.email_enabled);
    setEvents(incoming);
  }, [dirty, saving, settings]);

  useEffect(() => {
    let active = true;
    async function loadNotificationStatus(): Promise<void> {
      if (!window.pywebview?.api) {
        if (active) setServiceStatusLoading(false);
        return;
      }
      try {
        const result = await window.pywebview.api.notification_credentials_status();
        if (!active) return;
        if (result.status === "UNAVAILABLE") throw new Error(result.message);
        setServiceAvailable(result.service_available === true);
        setGatewayKeyConfigured(result.key_configured === true);
        setTelegramAvailable(result.telegram === true);
        setTelegramConnected(result.paired === true);
        setTelegramBotUsername(result.telegram_bot_username ?? "hugin_workbot");
        setEmailConfigured(result.email === true);
      } catch (reason) {
        if (active) setError(readableError(reason));
      } finally {
        if (active) setServiceStatusLoading(false);
      }
    }
    void loadNotificationStatus();
    return () => {
      active = false;
    };
  }, []);

  function toggleEvent(eventId: string): void {
    setEvents((current) =>
      current.includes(eventId)
        ? current.filter((value) => value !== eventId)
        : notificationEventOptions
            .map((option) => option.id)
            .filter((value) => value === eventId || current.includes(value)),
    );
    setError(null);
  }

  async function save(): Promise<void> {
    if (saving || !dirty) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await updateNotificationSettings(
        windowsEnabled,
        telegramEnabled,
        emailEnabled,
        events,
      );
      const savedSettings = updated.notification_settings;
      const savedEvents = selectedNotificationEvents(savedSettings);
      setBaselineChannels([
        savedSettings.windows_enabled,
        savedSettings.telegram_enabled,
        savedSettings.email_enabled,
      ]);
      setBaselineEvents(savedEvents);
      setWindowsEnabled(savedSettings.windows_enabled);
      setTelegramEnabled(savedSettings.telegram_enabled);
      setEmailEnabled(savedSettings.email_enabled);
      setEvents(savedEvents);
      onSaved(updated);
      onToast({ kind: "success", message: "Уведомления сохранены" });
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setSaving(false);
    }
  }

  async function connectTelegram(): Promise<void> {
    if (!window.pywebview?.api || credentialsSaving) {
      setError("Подключение Telegram доступно в оконном приложении Hugin.");
      return;
    }
    setCredentialsSaving(true);
    setError(null);
    try {
      const result = await window.pywebview.api.connect_telegram_notifications();
      if (result.status !== "READY") throw new Error(result.message);
      setServiceAvailable(result.service_available === true);
      setGatewayKeyConfigured(result.key_configured === true);
      setTelegramAvailable(result.telegram === true);
      setTelegramConnected(true);
      setTelegramEnabled(true);
      setTelegramBotUsername(result.telegram_bot_username ?? telegramBotUsername);
      onToast({
        kind: "success",
        message: `${result.message} Сохраните нужные виды уведомлений ниже.`,
      });
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setCredentialsSaving(false);
    }
  }

  async function testTelegram(): Promise<void> {
    if (!window.pywebview?.api || credentialsSaving) {
      setError("Проверка Telegram доступна в оконном приложении Hugin.");
      return;
    }
    setCredentialsSaving(true);
    setError(null);
    try {
      const result = await window.pywebview.api.test_telegram_notifications();
      if (result.status !== "READY") throw new Error(result.message);
      onToast({ kind: "success", message: result.message });
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setCredentialsSaving(false);
    }
  }

  async function testEmail(): Promise<void> {
    if (!window.pywebview?.api || credentialsSaving) {
      setError("Проверка почты доступна в оконном приложении Hugin.");
      return;
    }
    setCredentialsSaving(true);
    setError(null);
    try {
      const result = await window.pywebview.api.test_email_notifications();
      if (result.status !== "READY") throw new Error(result.message);
      setEmailConfigured(true);
      onToast({ kind: "success", message: result.message });
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setCredentialsSaving(false);
    }
  }

  return (
    <div className="notification-settings-form">
      <div className="notification-channel-grid">
        {[
          {
            title: "Windows",
            description: "Показывать на этом компьютере.",
            enabled: windowsEnabled,
            setEnabled: setWindowsEnabled,
            disabled: false,
          },
          {
            title: "Telegram",
            description: telegramConnected
              ? `Отправлять через @${telegramBotUsername}.`
              : "Сначала подключите чат через службу уведомлений.",
            enabled: telegramEnabled,
            setEnabled: setTelegramEnabled,
            disabled:
              !serviceAvailable ||
              !gatewayKeyConfigured ||
              !telegramAvailable ||
              !telegramConnected,
          },
          {
            title: "Электронная почта",
            description: emailConfigured
              ? "Отправлять через настроенный служебный ящик."
              : "Почта на службе уведомлений пока не готова.",
            enabled: emailEnabled,
            setEnabled: setEmailEnabled,
            disabled: !serviceAvailable || !gatewayKeyConfigured || !emailConfigured,
          },
        ].map((channel) => (
          <label className="notification-master" key={channel.title}>
            <span className="check-control">
              <input
                type="checkbox"
                checked={channel.enabled}
                disabled={channel.disabled}
                onChange={(event) => {
                  channel.setEnabled(event.target.checked);
                  setError(null);
                }}
              />
              <span aria-hidden="true" />
            </span>
            <span>
              <strong>{channel.title}</strong>
              <small>{channel.description}</small>
            </span>
          </label>
        ))}
      </div>

      <section className="notification-connection telegram-connection">
        <div className="telegram-connection-heading">
          <span>
            <strong>Служба уведомлений</strong>
            <small>Telegram и электронная почта</small>
          </span>
          <span
            className={
              serviceAvailable && gatewayKeyConfigured
                ? "status-pill positive"
                : !gatewayKeyConfigured
                  ? "status-pill warning"
                  : "status-pill"
            }
          >
            {serviceStatusLoading
              ? "Проверяем"
              : !gatewayKeyConfigured
                ? "Нет ключа связи"
                : serviceAvailable
                  ? "Работает"
                  : "Недоступна"}
          </span>
        </div>
        <div className="telegram-connect-actions">
          <p>
            Токен бота и почтовый пароль остаются в отдельной службе. Hugin получает только
            возможность отправлять подготовленные уведомления.
          </p>
        </div>

        <div className="telegram-connection-heading">
          <span>
            <strong>Telegram</strong>
            <small>@{telegramBotUsername}</small>
          </span>
          <span
            className={
              telegramConnected
                ? "status-pill positive"
                : telegramAvailable
                  ? "status-pill warning"
                  : "status-pill"
            }
          >
            {serviceStatusLoading
              ? "Проверяем"
              : telegramConnected
                ? "Подключён"
                : telegramAvailable
                  ? "Ждёт подключения"
                  : "Недоступен"}
          </span>
        </div>
        <div className="telegram-connected-actions">
          <p>
            {telegramConnected
              ? "Важные события можно отправлять в личный чат."
              : "Создайте одноразовую ссылку и нажмите «Старт» в Telegram."}
          </p>
          {telegramConnected && (
            <button
              type="button"
              className="secondary-button"
              disabled={credentialsSaving}
              onClick={() => void testTelegram()}
            >
              Проверить Telegram
            </button>
          )}
          {!telegramConnected && (
            <button
              type="button"
              className="primary-button"
              disabled={
                credentialsSaving ||
                !serviceAvailable ||
                !gatewayKeyConfigured ||
                !telegramAvailable
              }
              onClick={() => void connectTelegram()}
            >
              {credentialsSaving ? "Ждём нажатия «Старт»…" : "Подключить Telegram"}
            </button>
          )}
        </div>

        <div className="telegram-connection-heading">
          <span>
            <strong>Электронная почта</strong>
            <small>Служебный отправитель и получатель настроены отдельно</small>
          </span>
          <span className={emailConfigured ? "status-pill positive" : "status-pill"}>
            {serviceStatusLoading ? "Проверяем" : emailConfigured ? "Готова" : "Недоступна"}
          </span>
        </div>
        <div className="telegram-connected-actions">
          <p>
            Hugin не хранит почтовый пароль и не принимает адрес получателя через это окно.
          </p>
          <button
            type="button"
            className="secondary-button"
            disabled={
              credentialsSaving ||
              !serviceAvailable ||
              !gatewayKeyConfigured ||
              !emailConfigured
            }
            onClick={() => void testEmail()}
          >
            {credentialsSaving ? "Выполняем проверку…" : "Отправить проверочное письмо"}
          </button>
        </div>
      </section>

      <div className="notification-event-grid">
        {notificationEventOptions.map((event) => (
          <label
            className={`notification-event-option ${anyChannel ? "" : "disabled"}`}
            key={event.id}
          >
            <input
              type="checkbox"
              checked={events.includes(event.id)}
              disabled={!anyChannel}
              onChange={() => toggleEvent(event.id)}
            />
            <span>
              <strong>{event.title}</strong>
              <small>{event.description}</small>
            </span>
          </label>
        ))}
      </div>

      <div className="notification-settings-footer">
        <span className="notification-unavailable">
          Ключ связи используется только кодом Hugin и не передаётся в это окно. Токен бота и
          почтовый пароль остаются в отдельной службе.
        </span>
        {error && (
          <span className="settings-submit-error" role="alert">
            {error}
          </span>
        )}
        <button
          type="button"
          className="primary-button"
          disabled={saving || !dirty}
          onClick={() => void save()}
        >
          {saving ? "Сохраняем…" : "Сохранить уведомления"}
        </button>
      </div>
    </div>
  );
}

type DirectionDraft = {
  is_active: boolean;
  queries: string;
  regionAreas: string[];
  work_formats: WorkFormat[];
  employment_forms: EmploymentForm[];
  minimum_salary: string;
  desired_salary: string;
  remote_all_russia: boolean;
  schedule_minutes: string;
};

const workFormatNames: Record<WorkFormat, string> = {
  REMOTE: "Удалённо",
  ON_SITE: "В офисе",
  HYBRID: "Гибрид",
};

const employmentFormNames: Record<EmploymentForm, string> = {
  FULL: "Полная занятость",
  PART: "Частичная занятость",
  PROJECT: "Проектная работа",
  FLY_IN_FLY_OUT: "Вахта",
};

function directionDraft(direction: DirectionSummary): DirectionDraft {
  return {
    is_active: direction.is_active,
    queries: direction.queries.join("\n"),
    regionAreas: direction.regions.map((region) => region.area),
    work_formats: [...direction.work_formats],
    employment_forms: [...direction.employment_forms],
    minimum_salary: direction.minimum_salary?.toString() ?? "",
    desired_salary: direction.desired_salary?.toString() ?? "",
    remote_all_russia: direction.remote_all_russia,
    schedule_minutes: direction.schedule_minutes.toString(),
  };
}

function DirectionSettingsCard({
  direction,
  availableRegions,
  onSaved,
  onToast,
}: {
  direction: DirectionSummary;
  availableRegions: SearchRegion[];
  onSaved: (direction: DirectionSummary) => void;
  onToast: (toast: Toast) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [baseline, setBaseline] = useState<DirectionDraft>(() => directionDraft(direction));
  const [draft, setDraft] = useState<DirectionDraft>(() => directionDraft(direction));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dirty = JSON.stringify(draft) !== JSON.stringify(baseline);

  useEffect(() => {
    if (dirty) return;
    const next = directionDraft(direction);
    setBaseline(next);
    setDraft(next);
  }, [direction, dirty]);

  function toggleValue<T extends string>(values: T[], value: T): T[] {
    return values.includes(value)
      ? values.filter((item) => item !== value)
      : [...values, value];
  }

  function optionalPositiveInteger(value: string, label: string): number | null {
    if (!value.trim()) return null;
    if (!/^\d+$/.test(value) || Number(value) < 1) {
      throw new Error(`${label} должна быть положительным целым числом`);
    }
    return Number(value);
  }

  async function save(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (saving) return;
    try {
      const queries = draft.queries
        .split("\n")
        .map((value) => value.trim())
        .filter(Boolean);
      if (!queries.length) throw new Error("Укажите хотя бы один поисковый запрос");
      const regions = availableRegions.filter((region) =>
        draft.regionAreas.includes(region.area),
      );
      if (!regions.length) throw new Error("Выберите хотя бы один город или Россию");
      if (draft.remote_all_russia && !draft.work_formats.includes("REMOTE")) {
        throw new Error("Поиск по всей России можно включить только для удалённой работы");
      }
      if (!/^\d+$/.test(draft.schedule_minutes)) {
        throw new Error("Интервал поиска должен быть целым числом");
      }
      const scheduleMinutes = Number(draft.schedule_minutes);
      if (scheduleMinutes < 5 || scheduleMinutes > 1440) {
        throw new Error("Интервал поиска должен быть от 5 до 1440 минут");
      }
      const values: DirectionSettings = {
        is_active: draft.is_active,
        queries,
        regions,
        work_formats: draft.work_formats,
        employment_forms: draft.employment_forms,
        minimum_salary: optionalPositiveInteger(
          draft.minimum_salary,
          "Минимальная зарплата",
        ),
        desired_salary: optionalPositiveInteger(
          draft.desired_salary,
          "Желаемая зарплата",
        ),
        remote_all_russia: draft.remote_all_russia,
        schedule_minutes: scheduleMinutes,
      };
      if (
        values.minimum_salary !== null &&
        values.desired_salary !== null &&
        values.minimum_salary > values.desired_salary
      ) {
        throw new Error("Минимальная зарплата не может быть выше желаемой");
      }
      setSaving(true);
      setError(null);
      const saved = await updateDirection(direction.id, values);
      const next = directionDraft(saved);
      setBaseline(next);
      setDraft(next);
      setEditing(false);
      onSaved(saved);
      onToast({ kind: "success", message: `Направление «${saved.name}» сохранено` });
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setSaving(false);
    }
  }

  function cancel(): void {
    setDraft(baseline);
    setError(null);
    setEditing(false);
  }

  return (
    <article className={`direction-settings-card ${editing ? "editing" : ""}`}>
      <div className="direction-settings-summary">
        <div>
          <div className="direction-title-row">
            <strong>{direction.name}</strong>
            <span className={`status-pill ${direction.is_active ? "positive" : "muted"}`}>
              {direction.is_active ? "Включено" : "Выключено"}
            </span>
          </div>
          {direction.description && <span>{direction.description}</span>}
          <span>
            {plural(direction.queued, "вакансия", "вакансии", "вакансий")} в очереди
            {direction.rejected > 0
              ? ` · ${plural(direction.rejected, "отклонена", "отклонены", "отклонено")}`
              : ""}
          </span>
        </div>
        <button
          type="button"
          className="secondary-button"
          aria-expanded={editing}
          onClick={() => (editing ? cancel() : setEditing(true))}
        >
          <SlidersHorizontal size={17} aria-hidden="true" />
          {editing ? "Закрыть" : "Настроить"}
        </button>
      </div>

      {editing && (
        <form className="direction-settings-form" onSubmit={(event) => void save(event)}>
          <label className="direction-active-control">
            <span className="check-control">
              <input
                type="checkbox"
                checked={draft.is_active}
                onChange={(event) =>
                  setDraft((current) => ({ ...current, is_active: event.target.checked }))
                }
              />
              <span aria-hidden="true" />
            </span>
            <span>
              <strong>Искать вакансии по этому направлению</strong>
              <small>Выключение остановит новые поиски, но сохранит историю.</small>
            </span>
          </label>

          <label className="text-field direction-query-field">
            <span>Поисковые запросы</span>
            <textarea
              rows={Math.max(3, Math.min(6, draft.queries.split("\n").length))}
              value={draft.queries}
              onChange={(event) =>
                setDraft((current) => ({ ...current, queries: event.target.value }))
              }
            />
            <small>Один запрос в каждой строке.</small>
          </label>

          <fieldset className="direction-choice-group">
            <legend>Города и регионы</legend>
            <div className="direction-option-grid region-options">
              {availableRegions.map((region) => (
                <label key={region.area}>
                  <input
                    type="checkbox"
                    checked={draft.regionAreas.includes(region.area)}
                    onChange={() =>
                      setDraft((current) => ({
                        ...current,
                        regionAreas: toggleValue(current.regionAreas, region.area),
                      }))
                    }
                  />
                  <span>{region.name}</span>
                </label>
              ))}
            </div>
          </fieldset>

          <div className="direction-two-columns">
            <fieldset className="direction-choice-group">
              <legend>Формат работы</legend>
              <div className="direction-option-list">
                {(Object.keys(workFormatNames) as WorkFormat[]).map((format) => (
                  <label key={format}>
                    <input
                      type="checkbox"
                      checked={draft.work_formats.includes(format)}
                      onChange={() =>
                        setDraft((current) => ({
                          ...current,
                          work_formats: toggleValue(current.work_formats, format),
                        }))
                      }
                    />
                    <span>{workFormatNames[format]}</span>
                  </label>
                ))}
              </div>
              <label className="inline-option">
                <input
                  type="checkbox"
                  checked={draft.remote_all_russia}
                  disabled={!draft.work_formats.includes("REMOTE")}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      remote_all_russia: event.target.checked,
                    }))
                  }
                />
                <span>Удалённые вакансии по всей России</span>
              </label>
            </fieldset>

            <fieldset className="direction-choice-group">
              <legend>Занятость</legend>
              <div className="direction-option-list">
                {(Object.keys(employmentFormNames) as EmploymentForm[]).map((form) => (
                  <label key={form}>
                    <input
                      type="checkbox"
                      checked={draft.employment_forms.includes(form)}
                      onChange={() =>
                        setDraft((current) => ({
                          ...current,
                          employment_forms: toggleValue(current.employment_forms, form),
                        }))
                      }
                    />
                    <span>{employmentFormNames[form]}</span>
                  </label>
                ))}
              </div>
            </fieldset>
          </div>

          <div className="direction-number-grid">
            <label className="number-field">
              <span>Минимальная зарплата</span>
              <div className="number-input">
                <input
                  inputMode="numeric"
                  value={draft.minimum_salary}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      minimum_salary: event.target.value,
                    }))
                  }
                />
                <span>₽</span>
              </div>
            </label>
            <label className="number-field">
              <span>Желаемая зарплата</span>
              <div className="number-input">
                <input
                  inputMode="numeric"
                  value={draft.desired_salary}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      desired_salary: event.target.value,
                    }))
                  }
                />
                <span>₽</span>
              </div>
            </label>
            <label className="number-field">
              <span>Повторять поиск</span>
              <div className="number-input">
                <input
                  inputMode="numeric"
                  value={draft.schedule_minutes}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      schedule_minutes: event.target.value,
                    }))
                  }
                />
                <span>мин</span>
              </div>
            </label>
          </div>

          {error && (
            <p className="settings-submit-error" role="alert">
              {error}
            </p>
          )}
          <div className="settings-form-actions">
            <button type="submit" className="primary-button" disabled={saving || !dirty}>
              {saving ? "Сохраняем…" : "Сохранить направление"}
            </button>
            <button
              type="button"
              className="secondary-button"
              disabled={saving}
              onClick={cancel}
            >
              Отменить
            </button>
          </div>
        </form>
      )}
    </article>
  );
}

type QueueSettingsDraft = {
  daily_limit: string;
  delay_min_seconds: string;
  delay_max_seconds: string;
};

type QueueSettingsField = keyof QueueSettingsDraft;
type QueueSettingsErrors = Partial<Record<QueueSettingsField, string>>;

function queueSettingsFromDashboard(dashboard: Dashboard): QueueSettings {
  return {
    daily_limit: dashboard.daily_limit,
    delay_min_seconds: dashboard.delay_min_seconds,
    delay_max_seconds: dashboard.delay_max_seconds,
  };
}

function queueSettingsDraft(values: QueueSettings): QueueSettingsDraft {
  return {
    daily_limit: String(values.daily_limit),
    delay_min_seconds: String(values.delay_min_seconds),
    delay_max_seconds: String(values.delay_max_seconds),
  };
}

function sameQueueSettingsDraft(
  draft: QueueSettingsDraft,
  values: QueueSettings,
): boolean {
  return (
    draft.daily_limit === String(values.daily_limit) &&
    draft.delay_min_seconds === String(values.delay_min_seconds) &&
    draft.delay_max_seconds === String(values.delay_max_seconds)
  );
}

function parseQueueSettings(
  draft: QueueSettingsDraft,
): { values?: QueueSettings; errors: QueueSettingsErrors } {
  const errors: QueueSettingsErrors = {};
  const parsed: Partial<QueueSettings> = {};
  const fields: {
    key: QueueSettingsField;
    minimum: number;
    message: string;
  }[] = [
    {
      key: "daily_limit",
      minimum: 25,
      message: "Укажите целое число от 25",
    },
    {
      key: "delay_min_seconds",
      minimum: 0,
      message: "Укажите целое число от 0",
    },
    {
      key: "delay_max_seconds",
      minimum: 0,
      message: "Укажите целое число от 0",
    },
  ];

  for (const field of fields) {
    const raw = draft[field.key].trim();
    const value = Number(raw);
    if (
      !/^\d+$/.test(raw) ||
      !Number.isSafeInteger(value) ||
      value < field.minimum
    ) {
      errors[field.key] = field.message;
      continue;
    }
    parsed[field.key] = value;
  }

  if (
    parsed.delay_min_seconds !== undefined &&
    parsed.delay_max_seconds !== undefined &&
    parsed.delay_max_seconds < parsed.delay_min_seconds
  ) {
    errors.delay_max_seconds = "Пауза «до» не может быть меньше паузы «от»";
  }

  return Object.keys(errors).length
    ? { errors }
    : { values: parsed as QueueSettings, errors };
}

function QueueSettingsForm({
  dashboard,
  onSaved,
  onToast,
}: {
  dashboard: Dashboard;
  onSaved: (settings: QueueSettings) => void;
  onToast: (toast: Toast) => void;
}) {
  const initial = queueSettingsFromDashboard(dashboard);
  const [baseline, setBaseline] = useState<QueueSettings>(initial);
  const [draft, setDraft] = useState<QueueSettingsDraft>(() =>
    queueSettingsDraft(initial),
  );
  const [errors, setErrors] = useState<QueueSettingsErrors>({});
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const dailyRef = useRef<HTMLInputElement>(null);
  const delayMinRef = useRef<HTMLInputElement>(null);
  const delayMaxRef = useRef<HTMLInputElement>(null);
  const dirty = !sameQueueSettingsDraft(draft, baseline);
  const parsedDaily = /^\d+$/.test(draft.daily_limit.trim())
    ? Number(draft.daily_limit)
    : null;
  const highLimit =
    parsedDaily !== null && Number.isSafeInteger(parsedDaily) && parsedDaily > 50;

  useEffect(() => {
    if (dirty || saving) return;
    const incoming = queueSettingsFromDashboard(dashboard);
    setBaseline(incoming);
    setDraft(queueSettingsDraft(incoming));
  }, [
    dashboard.daily_limit,
    dashboard.delay_max_seconds,
    dashboard.delay_min_seconds,
    dirty,
    saving,
  ]);

  function change(field: QueueSettingsField, value: string): void {
    setDraft((current) => ({ ...current, [field]: value }));
    setErrors((current) => {
      if (!current[field]) return current;
      const next = { ...current };
      delete next[field];
      return next;
    });
    setSubmitError(null);
  }

  function reset(): void {
    setDraft(queueSettingsDraft(baseline));
    setErrors({});
    setSubmitError(null);
  }

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (saving) return;
    const checked = parseQueueSettings(draft);
    setErrors(checked.errors);
    setSubmitError(null);
    if (!checked.values) {
      const first = (
        [
          ["daily_limit", dailyRef],
          ["delay_min_seconds", delayMinRef],
          ["delay_max_seconds", delayMaxRef],
        ] as const
      ).find(([field]) => checked.errors[field]);
      window.requestAnimationFrame(() => first?.[1].current?.focus());
      return;
    }

    setSaving(true);
    try {
      const saved = await updateQueueSettings(checked.values);
      setBaseline(saved);
      setDraft(queueSettingsDraft(saved));
      setErrors({});
      onSaved(saved);
      onToast({ kind: "success", message: "Настройки откликов сохранены" });
    } catch (reason) {
      setSubmitError(readableError(reason));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      className="queue-settings-form"
      aria-busy={saving}
      noValidate
      onSubmit={(event) => void submit(event)}
    >
      <label className="number-field" htmlFor="daily-limit">
        <span>Откликов в день</span>
        <span className="number-input">
          <input
            ref={dailyRef}
            id="daily-limit"
            type="number"
            min="25"
            step="1"
            inputMode="numeric"
            value={draft.daily_limit}
            aria-invalid={!!errors.daily_limit}
            aria-describedby={errors.daily_limit ? "daily-limit-error" : undefined}
            onChange={(event) => change("daily_limit", event.target.value)}
          />
          <span>откликов</span>
        </span>
        {errors.daily_limit && (
          <small id="daily-limit-error" className="field-error">
            {errors.daily_limit}
          </small>
        )}
      </label>

      <div className="delay-fields">
        <label className="number-field" htmlFor="delay-min">
          <span>Пауза от</span>
          <span className="number-input">
            <input
              ref={delayMinRef}
              id="delay-min"
              type="number"
              min="0"
              step="1"
              inputMode="numeric"
              value={draft.delay_min_seconds}
              aria-invalid={!!errors.delay_min_seconds}
              aria-describedby={
                errors.delay_min_seconds ? "delay-min-error" : "delay-hint"
              }
              onChange={(event) =>
                change("delay_min_seconds", event.target.value)
              }
            />
            <span>секунд</span>
          </span>
          {errors.delay_min_seconds && (
            <small id="delay-min-error" className="field-error">
              {errors.delay_min_seconds}
            </small>
          )}
        </label>
        <label className="number-field" htmlFor="delay-max">
          <span>Пауза до</span>
          <span className="number-input">
            <input
              ref={delayMaxRef}
              id="delay-max"
              type="number"
              min="0"
              step="1"
              inputMode="numeric"
              value={draft.delay_max_seconds}
              aria-invalid={!!errors.delay_max_seconds}
              aria-describedby={
                errors.delay_max_seconds ? "delay-max-error" : "delay-hint"
              }
              onChange={(event) =>
                change("delay_max_seconds", event.target.value)
              }
            />
            <span>секунд</span>
          </span>
          {errors.delay_max_seconds && (
            <small id="delay-max-error" className="field-error">
              {errors.delay_max_seconds}
            </small>
          )}
        </label>
      </div>

      <p id="delay-hint" className="field-hint">
        После каждого отклика программа случайно выбирает паузу в этом диапазоне.
      </p>

      {highLimit && (
        <div className="settings-warning" aria-live="polite">
          <AlertTriangle size={18} aria-hidden="true" />
          <span>
            Больше 50 откликов в день может повысить риск ограничений со стороны
            hh.ru. Значение можно сохранить.
          </span>
        </div>
      )}

      {submitError && (
        <p className="settings-submit-error" role="alert">
          {submitError}
        </p>
      )}

      <div className="settings-form-actions">
        <button
          type="submit"
          className="primary-button"
          disabled={!dirty || saving}
        >
          {saving ? "Сохраняем…" : "Сохранить"}
        </button>
        <button
          type="button"
          className="quiet-button"
          disabled={!dirty || saving}
          onClick={reset}
        >
          Отменить изменения
        </button>
      </div>
    </form>
  );
}

function WidgetSelector({
  widgets,
  onToggle,
}: {
  widgets: DashboardWidget[];
  onToggle: (widget: DashboardWidget) => void;
}) {
  return (
    <fieldset className="widget-selector">
      <legend className="sr-only">Блоки на главной</legend>
      {dashboardWidgetDefinitions.map((widget) => (
        <label key={widget.id}>
          <span className="check-control">
            <input
              type="checkbox"
              checked={widgets.includes(widget.id)}
              onChange={() => onToggle(widget.id)}
            />
            <span aria-hidden="true" />
          </span>
          <span>
            <strong>{widget.label}</strong>
            <small>{widget.description}</small>
          </span>
        </label>
      ))}
    </fieldset>
  );
}

function PreferencesDialog({
  open,
  widgets,
  onClose,
  onToggle,
  onReset,
}: {
  open: boolean;
  widgets: DashboardWidget[];
  onClose: () => void;
  onToggle: (widget: DashboardWidget) => void;
  onReset: () => void;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  useDialog(open, dialogRef, closeButtonRef, onClose);
  if (!open) return null;

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        ref={dialogRef}
        className="preferences-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="preferences-title"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="dialog-header">
          <div>
            <span className="eyebrow">Главная</span>
            <h2 id="preferences-title">Выберите важные блоки</h2>
            <p>Состояние очереди и предупреждения остаются видны всегда.</p>
          </div>
          <button
            ref={closeButtonRef}
            type="button"
            className="icon-button"
            aria-label="Закрыть настройку"
            onClick={onClose}
          >
            <X size={20} aria-hidden="true" />
          </button>
        </div>
        <WidgetSelector widgets={widgets} onToggle={onToggle} />
        <div className="dialog-actions">
          <button type="button" className="quiet-button" onClick={onReset}>
            <RotateCcw size={17} aria-hidden="true" />
            Вернуть стандартный вид
          </button>
          <button type="button" className="primary-button" onClick={onClose}>
            Готово
          </button>
        </div>
      </div>
    </div>
  );
}

function VacancyDrawer({
  vacancy,
  returnFocusRef,
  onClose,
  onToast,
}: {
  vacancy: VacancyCard | null;
  returnFocusRef: RefObject<HTMLElement | null>;
  onClose: () => void;
  onToast: (toast: Toast) => void;
}) {
  const drawerRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const [openingSource, setOpeningSource] = useState(false);
  useDialog(!!vacancy, drawerRef, closeButtonRef, onClose, returnFocusRef);

  async function openSource(): Promise<void> {
    if (!vacancy || openingSource) return;
    setOpeningSource(true);
    try {
      if (window.pywebview?.api) {
        const result = await window.pywebview.api.open_url(vacancy.source_url);
        if (result.status !== "ok") throw new Error(result.message);
      } else {
        window.open(vacancy.source_url, "_blank", "noopener,noreferrer");
      }
    } catch (reason) {
      onToast({ kind: "error", message: readableError(reason) });
    } finally {
      setOpeningSource(false);
    }
  }

  if (!vacancy) return null;
  const reasons = visibleReasons(vacancy.reasons);
  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside
        ref={drawerRef}
        className="vacancy-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="vacancy-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="drawer-header">
          <button
            ref={closeButtonRef}
            type="button"
            className="icon-button"
            aria-label="Закрыть карточку вакансии"
            onClick={onClose}
          >
            <X size={20} aria-hidden="true" />
          </button>
          <button
            type="button"
            className="secondary-button"
            disabled={openingSource}
            onClick={() => void openSource()}
          >
            <ExternalLink size={17} aria-hidden="true" />
            {openingSource ? "Открываем…" : "Открыть на hh.ru"}
          </button>
        </header>

        <div className="drawer-title">
          <span className="eyebrow">{vacancy.company}</span>
          <h2 id="vacancy-title">{vacancy.title}</h2>
          <p>
            {vacancy.region} · {vacancy.work_format} · {vacancy.salary}
          </p>
          <div className="drawer-badges">
            <span className={`status-pill ${stateTone(vacancy.state)}`}>
              {stateNames[vacancy.state] ?? "Состояние уточняется"}
            </span>
            {vacancy.score !== null && (
              <span className="score-badge">Оценка {Math.round(vacancy.score)}</span>
            )}
          </div>
        </div>

        <div className="drawer-content">
          <details className="drawer-section" open>
            <summary>
              <span>Главное</span>
              <ChevronDown size={18} aria-hidden="true" />
            </summary>
            <dl className="vacancy-facts">
              <div>
                <dt>Направление</dt>
                <dd>{vacancy.direction}</dd>
              </div>
              <div>
                <dt>Опыт</dt>
                <dd>{vacancy.experience}</dd>
              </div>
              <div>
                <dt>Занятость</dt>
                <dd>{vacancy.employment}</dd>
              </div>
              <div>
                <dt>Адрес</dt>
                <dd>{vacancy.address}</dd>
              </div>
            </dl>
            {!!reasons.length && (
              <div className="reason-box">
                <strong>Почему принято такое решение</strong>
                <ul>
                  {reasons.map((reason) => (
                    <li key={reason}>{reason}</li>
                  ))}
                </ul>
              </div>
            )}
            {!!vacancy.discoveries.length && (
              <div className="discovery-note">
                Найдено по запросу: {vacancy.discoveries.join("; ")}
              </div>
            )}
          </details>

          {(vacancy.cover_letter || vacancy.questions.length > 0) && (
            <details className="drawer-section" open>
              <summary>
                <span>Отклик и анкета</span>
                <ChevronDown size={18} aria-hidden="true" />
              </summary>
              {vacancy.cover_letter && (
                <div className="letter-box">
                  <strong>Сопроводительное письмо</strong>
                  <p>{vacancy.cover_letter}</p>
                </div>
              )}
              {!!vacancy.questions.length && (
                <ol className="drawer-questions">
                  {vacancy.questions.map((question, index) => (
                    <li key={`${question.text}-${index}`}>
                      <strong>{question.text}</strong>
                      <p>{question.answer || "Ответ ещё не указан"}</p>
                      {question.source && (
                        <small>{sourceNames[question.source] ?? question.source}</small>
                      )}
                    </li>
                  ))}
                </ol>
              )}
            </details>
          )}

          <details className="drawer-section">
            <summary>
              <span>Описание и навыки</span>
              <ChevronDown size={18} aria-hidden="true" />
            </summary>
            {!!vacancy.skills.length && (
              <div className="skill-list">
                {vacancy.skills.map((skill) => (
                  <span key={skill}>{skill}</span>
                ))}
              </div>
            )}
            <div className="vacancy-description">{vacancy.description}</div>
          </details>

          {!!vacancy.events.length && (
            <details className="drawer-section">
              <summary>
                <span>История</span>
                <ChevronDown size={18} aria-hidden="true" />
              </summary>
              <ol className="history-list">
                {vacancy.events.map((event, index) => (
                  <li key={`${event.event_type}-${event.created_at}-${index}`}>
                    <span aria-hidden="true" />
                    <div>
                      <strong>{stateNames[event.event_type] ?? "Событие"}</strong>
                      <small>{formatDate(event.created_at, true)}</small>
                      {event.details && <p>{event.details}</p>}
                    </div>
                  </li>
                ))}
              </ol>
            </details>
          )}
        </div>
      </aside>
    </div>
  );
}

function ToastMessage({ toast, onClose }: { toast: Toast; onClose: () => void }) {
  return (
    <div
      className={`toast ${toast.kind}`}
      role={toast.kind === "error" ? "alert" : "status"}
      aria-live={toast.kind === "error" ? "assertive" : "polite"}
    >
      {toast.kind === "success" ? (
        <CheckCircle2 size={20} aria-hidden="true" />
      ) : (
        <AlertTriangle size={20} aria-hidden="true" />
      )}
      <span>{toast.message}</span>
      <button type="button" aria-label="Закрыть сообщение" onClick={onClose}>
        <X size={17} aria-hidden="true" />
      </button>
    </div>
  );
}

function stateTone(state: string): string {
  if (
    [
      "RUNNING",
      "READY",
      "COMPLETED",
      "CONFIRMED",
      "SENT",
      "APPLIED",
      "VIEWED",
      "INVITED",
      "RECEIVED",
    ].includes(state)
  ) {
    return "positive";
  }
  if (
    ["RETRY_SCHEDULED", "REVIEW_REQUIRED", "INPUT_REQUIRED", "SCHEDULED", "PREPARING"].includes(
      state,
    )
  ) {
    return "warning";
  }
  if (["FAILED", "UNKNOWN_RESULT"].includes(state)) return "danger";
  return "muted";
}
