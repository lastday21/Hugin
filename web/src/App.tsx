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
  RefreshCw,
  RotateCcw,
  Search,
  Settings,
  SlidersHorizontal,
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
  loadVacancy,
  loadWorkspace,
  updateQueueSettings,
} from "./api";
import {
  dashboardWidgetDefinitions,
  defaultDashboardWidgets,
  loadDashboardWidgets,
  saveDashboardWidgets,
  type DashboardWidget,
} from "./dashboardPreferences";
import type {
  Dashboard,
  FormDraft,
  QueueItem,
  QueueSettings,
  RejectedVacancy,
  SystemState,
  VacancyCard,
} from "./types";

type View = "dashboard" | "vacancies" | "attention" | "settings";
type VacancyTab = "queue" | "rejected";
type AttentionTab = "input" | "review";
type Toast = { kind: "success" | "error"; message: string };

interface Workspace {
  dashboard: Dashboard;
  queue: QueueItem[];
  forms: FormDraft[];
  rejected: RejectedVacancy[];
}

const navigation: {
  id: View;
  label: string;
  icon: typeof Home;
}[] = [
  { id: "dashboard", label: "Главная", icon: Home },
  { id: "vacancies", label: "Вакансии", icon: BriefcaseBusiness },
  { id: "attention", label: "Требует внимания", icon: Bell },
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
  DRAFT: "Черновик",
  INVALIDATED: "Устарело",
  APPLY_INTENT: "Начат отклик",
  APPLIED: "Отклик отправлен",
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
                  rejected={workspace.rejected}
                  loading={vacancyLoading}
                  onOpenVacancy={openVacancy}
                />
              )}
              {view === "attention" && (
                <AttentionView forms={workspace.forms} onToast={showToast} />
              )}
              {view === "settings" && (
                <SettingsView
                  dashboard={workspace.dashboard}
                  widgets={widgets}
                  onToggleWidget={toggleWidget}
                  onResetWidgets={resetWidgets}
                  onSettingsSaved={applyQueueSettings}
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

  async function controlQueue(action: "pause" | "resume"): Promise<void> {
    if (changingState) return;
    setChangingState(true);
    try {
      await changeQueueState(action);
      onToast({
        kind: "success",
        message: action === "pause" ? "Очередь поставлена на паузу" : "Очередь продолжила работу",
      });
      onRefresh();
    } catch (reason) {
      onToast({ kind: "error", message: readableError(reason) });
    } finally {
      setChangingState(false);
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
          {dashboard.system_state === "RUNNING" && (
            <button
              type="button"
              className="secondary-button"
              disabled={changingState}
              onClick={() => void controlQueue("pause")}
            >
              <CirclePause size={19} aria-hidden="true" />
              {changingState ? "Ставим на паузу…" : "Поставить на паузу"}
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
              {changingState ? "Запускаем…" : "Продолжить работу"}
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

function systemPresentation(state: SystemState, queueLength: number) {
  const queueText = queueLength
    ? `${plural(queueLength, "вакансия", "вакансии", "вакансий")} ждут обработки`
    : "Новых вакансий в очереди нет";
  switch (state) {
    case "RUNNING":
      return {
        title: "Очередь включена",
        description: queueText,
        tone: "positive",
        icon: CheckCircle2,
      };
    case "PAUSED":
      return {
        title: "Очередь на паузе",
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
  rejected,
  loading,
  onOpenVacancy,
}: {
  queue: QueueItem[];
  rejected: RejectedVacancy[];
  loading: boolean;
  onOpenVacancy: (vacancyId: string) => Promise<void>;
}) {
  const [tab, setTab] = useState<VacancyTab>("queue");
  const [search, setSearch] = useState("");
  const searchInputRef = useRef<HTMLInputElement>(null);
  const queueTabRef = useRef<HTMLButtonElement>(null);
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

  function onTabKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>): void {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const nextTab =
      event.key === "Home"
        ? "queue"
        : event.key === "End"
          ? "rejected"
          : tab === "queue"
            ? "rejected"
            : "queue";
    setTab(nextTab);
    window.requestAnimationFrame(() =>
      (nextTab === "queue" ? queueTabRef : rejectedTabRef).current?.focus(),
    );
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
            <SearchEmpty hasSearch={!!normalizedSearch} kind="queue" />
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
            : "Отклонённых вакансий нет"
      }
      description={
        hasSearch
          ? "Попробуйте изменить запрос."
          : kind === "queue"
            ? "Новые подходящие вакансии появятся здесь."
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

function SettingsView({
  dashboard,
  widgets,
  onToggleWidget,
  onResetWidgets,
  onSettingsSaved,
  onToast,
}: {
  dashboard: Dashboard;
  widgets: DashboardWidget[];
  onToggleWidget: (widget: DashboardWidget) => void;
  onResetWidgets: () => void;
  onSettingsSaved: (settings: QueueSettings) => void;
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
        <ul className="settings-directions">
          {dashboard.directions.map((direction) => (
            <li key={direction.id}>
              <div>
                <strong>{direction.name}</strong>
                {direction.description && <span>{direction.description}</span>}
                <span>
                  {plural(direction.queued, "вакансия", "вакансии", "вакансий")} в очереди
                </span>
              </div>
              <span className={`status-pill ${direction.is_active ? "positive" : "muted"}`}>
                {direction.is_active ? "Включено" : "Выключено"}
              </span>
            </li>
          ))}
        </ul>
      </section>
    </div>
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
  if (["RUNNING", "READY", "COMPLETED", "CONFIRMED", "SENT"].includes(state)) {
    return "positive";
  }
  if (["RETRY_SCHEDULED", "REVIEW_REQUIRED", "INPUT_REQUIRED"].includes(state)) {
    return "warning";
  }
  if (["FAILED", "UNKNOWN_RESULT"].includes(state)) return "danger";
  return "muted";
}
