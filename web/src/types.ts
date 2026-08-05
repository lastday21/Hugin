export type SystemState =
  | "RUNNING"
  | "PAUSED"
  | "AUTH_REQUIRED"
  | "CAPTCHA_REQUIRED"
  | "ACCOUNT_WARNING";

export type WorkFormat = "REMOTE" | "ON_SITE" | "HYBRID";
export type EmploymentForm = "FULL" | "PART" | "PROJECT" | "FLY_IN_FLY_OUT";

export interface SearchRegion {
  area: string;
  name: string;
}

export interface DirectionSummary {
  id: number;
  name: string;
  description: string | null;
  role_scope: "PYTHON_BACKEND" | "IT_ADJACENT";
  is_active: boolean;
  queued: number;
  rejected: number;
  queries: string[];
  regions: SearchRegion[];
  work_formats: WorkFormat[];
  employment_forms: EmploymentForm[];
  minimum_salary: number | null;
  desired_salary: number | null;
  remote_all_russia: boolean;
  schedule_minutes: number;
}

export interface DirectionOptions {
  regions: SearchRegion[];
}

export interface DirectionSettings {
  is_active: boolean;
  queries: string[];
  regions: SearchRegion[];
  work_formats: WorkFormat[];
  employment_forms: EmploymentForm[];
  minimum_salary: number | null;
  desired_salary: number | null;
  remote_all_russia: boolean;
  schedule_minutes: number;
}

export interface Incident {
  id: number;
  code: string;
  severity: string;
  message: string;
  created_at: string;
}

export interface BackgroundStatus {
  state: "NOT_STARTED" | "RUNNING" | "NEEDS_ATTENTION" | "STOPPED";
  last_success_at: string | null;
  next_search_at: string | null;
  next_messages_at: string | null;
  next_statuses_at: string | null;
  error: string | null;
}

export interface Dashboard {
  account_label: string;
  system_state: SystemState;
  search_enabled: boolean;
  resource_saving_mode: boolean;
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
  background: BackgroundStatus;
  directions: DirectionSummary[];
  incidents: Incident[];
}

export interface QueueSettings {
  daily_limit: number;
  delay_min_seconds: number;
  delay_max_seconds: number;
}

export interface ProfileResume {
  id: number;
  hh_id: string;
  title: string;
  source_type: string | null;
  source_original_name: string | null;
  source_size_bytes: number | null;
  source_page_count: number | null;
  imported_at: string | null;
}

export interface ProfileFact {
  id: number;
  category: string;
  content: string;
  source_type: string;
  source_reference: string | null;
  state: "PENDING" | "CONFIRMED" | "REJECTED";
  allow_in_letters: boolean;
  allow_in_forms: boolean;
  allow_in_messages: boolean;
  created_at: string;
  updated_at: string;
}

export interface ProfileQuestion {
  key: string;
  question: string;
  answer: string | null;
  state: "PENDING" | "ANSWERED" | "DISMISSED";
}

export interface AnswerTemplate {
  key: string;
  question: string;
  answer: string;
}

export interface Profile {
  account_label: string;
  display_name: string;
  active_resume: ProfileResume | null;
  facts: ProfileFact[];
  questions: ProfileQuestion[];
  answers: AnswerTemplate[];
}

export interface ResumePreviewFact {
  category: string;
  content: string;
}

export interface ResumePreviewQuestion {
  key: string;
  question: string;
}

export interface ResumePreview {
  token: string;
  original_name: string;
  source_type: string;
  title: string;
  page_count: number | null;
  facts: ResumePreviewFact[];
  questions: ResumePreviewQuestion[];
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
  decision_reasons: string[];
}

export interface SentApplication {
  application_id: number;
  vacancy_id: string;
  title: string;
  company: string;
  region: string;
  source_url: string;
  resume_title: string;
  direction: string;
  state: string;
  applied_at: string;
}

export interface RecruiterMessage {
  id: number;
  direction: "INCOMING" | "OUTGOING";
  body: string;
  state: string;
  occurred_at: string;
  read_at: string | null;
  content_hash: string | null;
  content_version: number;
}

export interface Conversation {
  application_id: number;
  vacancy_id: string;
  vacancy_title: string;
  company: string;
  source_url: string;
  unread_count: number;
  needs_reply: boolean;
  messages: RecruiterMessage[];
}

export interface CommunicationInvitation {
  id: number;
  application_id: number;
  vacancy_id: string;
  vacancy_title: string;
  company: string;
  source_url: string;
  title: string;
  details: string | null;
  interview_at: string | null;
  booking_url: string | null;
  state: string;
  seen_at: string | null;
  created_at: string;
}

export interface Communications {
  conversations: Conversation[];
  invitations: CommunicationInvitation[];
  unread_messages: number;
  unseen_invitations: number;
  notification_settings: NotificationSettings;
  ai_model_settings: AiModelSettings;
  ai_prompt_settings: AiPromptSettings;
}

export interface NotificationSettings {
  windows_enabled: boolean;
  telegram_enabled: boolean;
  email_enabled: boolean;
  routing: Record<string, string[]>;
}

export interface AiPromptValues {
  resume: string;
  cover_letter: string;
  recruiter_reply: string;
}

export interface AiPromptSettings extends AiPromptValues {
  defaults: AiPromptValues;
}

export interface AiModelOption {
  value: string;
  title: string;
  description: string;
}

export interface AiModelSettings {
  selected: string;
  options: AiModelOption[];
  reasoning_effort: string;
  reasoning_options: AiModelOption[];
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
  service_available?: boolean;
  key_configured?: boolean;
  telegram?: boolean | null;
  paired?: boolean | null;
  email?: boolean | null;
  telegram_bot_username?: string;
  body?: string;
}

declare global {
  interface Window {
    pywebview?: {
      api: {
        open_form: (vacancyId: string) => Promise<BridgeResult>;
        open_invitation: (invitationId: number) => Promise<BridgeResult>;
        open_url: (url: string) => Promise<BridgeResult>;
        send_reply: (
          messageId: number,
          contentHash: string,
          contentVersion: number,
        ) => Promise<BridgeResult>;
        generate_reply: (applicationId: number) => Promise<BridgeResult>;
        notification_credentials_status: () => Promise<BridgeResult>;
        connect_telegram_notifications: () => Promise<BridgeResult>;
        test_telegram_notifications: () => Promise<BridgeResult>;
        test_email_notifications: () => Promise<BridgeResult>;
      };
    };
  }
}
