export type SystemState =
  | "RUNNING"
  | "PAUSED"
  | "AUTH_REQUIRED"
  | "CAPTCHA_REQUIRED"
  | "ACCOUNT_WARNING";

export interface DirectionSummary {
  id: number;
  name: string;
  description: string | null;
  is_active: boolean;
  queued: number;
  rejected: number;
}

export interface Incident {
  id: number;
  code: string;
  severity: string;
  message: string;
  created_at: string;
}

export interface Dashboard {
  account_label: string;
  system_state: SystemState;
  next_apply_at: string | null;
  daily_limit: number;
  delay_min_seconds: number;
  delay_max_seconds: number;
  applied_today: number;
  remaining_today: number;
  task_counts: Record<string, number>;
  pending_forms: number;
  ready_letters: number;
  rejected_vacancies: number;
  new_messages: number;
  invitations: number;
  directions: DirectionSummary[];
  incidents: Incident[];
}

export interface QueueSettings {
  daily_limit: number;
  delay_min_seconds: number;
  delay_max_seconds: number;
}

export interface QueueItem {
  task_id: number;
  vacancy_id: string;
  title: string;
  company: string;
  region: string;
  source_url: string;
  resume_title: string;
  direction: string;
  state: string;
  priority: number;
  scheduled_at: string;
  last_error: string | null;
  letter_state: string | null;
  form_state: string | null;
}

export interface FormQuestion {
  field_key: string;
  question: string;
  field_type: string;
  is_required: boolean;
  options: string[];
  answer: string | null;
  source: string | null;
}

export interface FormDraft {
  form_id: number;
  application_id: number;
  vacancy_id: string;
  vacancy_title: string;
  company: string;
  source_url: string;
  resume_title: string;
  state: string;
  answered_count: number;
  unanswered_count: number;
  questions: FormQuestion[];
}

export interface RejectedVacancy {
  vacancy_id: string;
  title: string;
  company: string;
  region: string;
  source_url: string;
  direction: string;
  score: number | null;
  reasons: string[];
}

export interface VacancyQuestion {
  text: string;
  answer: string | null;
  source: string | null;
  required: boolean;
}

export interface VacancyEvent {
  event_type: string;
  created_at: string;
  details: string;
}

export interface VacancyCard {
  vacancy_id: string;
  title: string;
  company: string;
  source_url: string;
  region: string;
  address: string;
  salary: string;
  employment: string;
  work_format: string;
  experience: string;
  skills: string[];
  description: string;
  direction: string;
  state: string;
  score: number | null;
  reasons: string[];
  discoveries: string[];
  cover_letter: string | null;
  form_state: string | null;
  questions: VacancyQuestion[];
  events: VacancyEvent[];
}

export interface BridgeResult {
  status: string;
  message: string;
  filled?: number;
  skipped?: number;
}

declare global {
  interface Window {
    pywebview?: {
      api: {
        open_form: (vacancyId: string) => Promise<BridgeResult>;
        open_url: (url: string) => Promise<BridgeResult>;
      };
    };
  }
}
