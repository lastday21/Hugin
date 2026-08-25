export type DashboardWidget =
  | "attention"
  | "queue"
  | "directions";

export interface DashboardWidgetDefinition {
  id: DashboardWidget;
  label: string;
  description: string;
}

export const dashboardWidgetDefinitions: DashboardWidgetDefinition[] = [
  {
    id: "attention",
    label: "Требует внимания",
    description: "Анкеты, которые ждут вашего решения",
  },
  {
    id: "queue",
    label: "Ближайшие вакансии",
    description: "Первые вакансии из очереди",
  },
  {
    id: "directions",
    label: "Направления",
    description: "Активные направления и их состояние",
  },
];

export const defaultDashboardWidgets: DashboardWidget[] = [
  "attention",
  "queue",
];

const storageKey = "hugin.dashboard.widgets.v1";
const knownWidgets = new Set<DashboardWidget>(
  dashboardWidgetDefinitions.map((widget) => widget.id),
);

export function loadDashboardWidgets(): DashboardWidget[] {
  try {
    const stored = window.localStorage.getItem(storageKey);
    if (!stored) return defaultDashboardWidgets;
    const parsed = JSON.parse(stored) as unknown;
    if (!Array.isArray(parsed)) return defaultDashboardWidgets;
    return parsed.filter(
      (value): value is DashboardWidget =>
        typeof value === "string" && knownWidgets.has(value as DashboardWidget),
    );
  } catch {
    return defaultDashboardWidgets;
  }
}

export function saveDashboardWidgets(widgets: DashboardWidget[]): void {
  try {
    window.localStorage.setItem(storageKey, JSON.stringify(widgets));
  } catch {
    // Настройка внешнего вида не должна мешать основной работе приложения.
  }
}
