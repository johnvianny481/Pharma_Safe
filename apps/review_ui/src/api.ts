/** Calls the PharmaSafe API through the dev-server proxy (`/api` → API). */

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly detail?: unknown,
  ) {
    super(message);
  }
}

function describe(status: number, detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail) {
    return String((detail as { message: unknown }).message);
  }
  return `API returned ${status}`;
}

export async function apiRequest<T>(
  method: string,
  path: string,
  accessToken: string,
  body?: unknown,
  idempotencyKey?: string,
): Promise<T> {
  const headers: Record<string, string> = { Authorization: `Bearer ${accessToken}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  const response = await fetch(`/api${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let detail: unknown;
    try {
      detail = ((await response.json()) as { detail?: unknown }).detail;
    } catch {
      detail = undefined;
    }
    throw new ApiError(response.status, describe(response.status, detail), detail);
  }
  return (await response.json()) as T;
}

export const apiGet = <T>(path: string, accessToken: string) =>
  apiRequest<T>("GET", path, accessToken);

/** POST JSON. Pass an idempotency key for intake calls, and reuse it when retrying. */
export const apiPost = <T>(
  path: string,
  body: unknown,
  accessToken: string,
  idempotencyKey?: string,
) => apiRequest<T>("POST", path, accessToken, body, idempotencyKey);

// ---------------------------------------------------------------- types

export interface IntakeAck {
  submission_id: string;
  duplicate: boolean;
  reports: { index: number; case_id: string; case_number: string }[];
}

export interface Me {
  subject: string;
  username: string;
  display_name: string;
  tenant_id: string | null;
  roles: string[];
  /** `null` means unrestricted. */
  products: string[] | null;
  regions: string[] | null;
}

export interface Evidence {
  document_id: string;
  start: number;
  end: number;
  text: string;
}

/** A case field with its provenance. */
export interface Tracked<T = unknown> {
  value: T;
  source: "ai" | "human" | "import";
  evidence: Evidence | null;
  confidence: number | null;
  confirmed_by: string | null;
  confirmed_at: string | null;
}

export interface Task {
  id: string;
  case_id: string;
  case_number: string | null;
  case_version: number;
  kind: "data_entry" | "quality_review" | "medical_review" | "approval";
  status: string;
  assignee: string | null;
  created_at: string;
  next_due: string | null;
  case_stage: string | null;
  flags: string[];
}

export interface VersionSummary {
  version: number;
  status: "draft" | "approved";
  revision: number;
  content_hash: string;
  created_at: string;
  created_by: string;
  approved_at: string | null;
  approved_by: string | null;
}

export interface CaseDetail {
  id: string;
  case_number: string;
  region: string;
  day_zero: string | null;
  latest_version: number;
  created_at: string;
  workflow_stage: string;
  merged_into: string | null;
  versions: VersionSummary[];
}

/** E2B-structured case content (see pharmasafe_domain.case). */
export type CaseContent = Record<string, unknown> & {
  admin: Record<string, Tracked | null>;
  patient: Record<string, Tracked | null>;
  reporters: Item[];
  drugs: Item[];
  reactions: Item[];
  tests: Item[];
  narrative: Record<string, Tracked | null>;
};

export type Item = { id: string } & Record<string, unknown>;

export interface VersionDetail extends VersionSummary {
  content: CaseContent;
}

export interface Issue {
  code: string;
  path: string;
  message: string;
  severity: string;
}

export interface Assessment {
  valid: boolean;
  missing_criteria: string[];
  serious: boolean;
  issues: Issue[];
  approval_blockers: Issue[];
}

export interface SourceText {
  document_id: string;
  filename: string | null;
  content_type: string;
  text: string;
  report_start: number;
  report_end: number;
  language: string;
}

export interface DuplicateCandidate {
  case_id: string;
  case_number: string;
  score: number;
  reasons: string[];
}

export interface AiRun {
  kind: "pipeline" | "narrative";
  bundle_version: string;
  status: string;
  automation_level: string;
  flags: string[];
  created_at: string;
  result: {
    seriousness?: {
      llm: Record<string, boolean>;
      rules: Record<string, boolean>;
      met: Record<string, boolean>;
      disagreements: string[];
    };
    duplicates?: DuplicateCandidate[];
    follow_up_questions?: { path: string; question: string }[];
    coding?: { item: string; kind: string; term: string; code: string; label: string }[];
    rejections?: { path: string; reason: string }[];
    proposals?: { op: string; path: string; value?: Tracked | null; item?: Item }[];
    automation?: { level: string; reasons: string[] };
    draft?: string;
    [key: string]: unknown;
  };
}

export interface CodingCandidate {
  code: string;
  label: string;
  score: number;
}

export interface TenantSettings {
  slug: string;
  name: string;
  region: string;
  regulators: string[];
  available_regulators: string[];
  ai_automation_level: string;
  qc_sample_rate: string;
  intake_mailbox: string | null;
  ai_monthly_budget_usd: string;
}

export interface AuditEntry {
  seq: number;
  occurred_at: string;
  actor_id: string;
  actor_name: string;
  action: string;
  field: string | null;
  old_value: unknown;
  new_value: unknown;
  reason: string | null;
  case_version: number | null;
}

export type EditOp =
  | { op: "set"; path: string; value: unknown }
  | { op: "confirm"; path: string }
  | { op: "add"; path: string; id: string }
  | { op: "remove"; path: string };
