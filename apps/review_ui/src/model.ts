/**
 * The E2B(R3)-structured case form: which fields each section has and how each is edited.
 * Field names and codes follow pharmasafe_domain.case.
 */
import type { CaseContent, Item, Tracked } from "./api";

export type FieldKind =
  | { type: "text"; multiline?: boolean }
  | { type: "date" }
  | { type: "number" }
  | { type: "enum"; options: readonly (readonly [string, string])[] }
  | { type: "age" }
  | { type: "dose" }
  | { type: "country" }
  | { type: "meddra" };

export interface FieldSpec {
  name: string;
  label: string;
  kind: FieldKind;
}

const text = { type: "text" } as const;
const date = { type: "date" } as const;
const enumOf = (...options: (readonly [string, string])[]) => ({ type: "enum", options }) as const;

export const SEX = enumOf(["female", "Female"], ["male", "Male"], ["unknown", "Unknown"]);
export const QUALIFICATION = enumOf(
  ["physician", "Physician"],
  ["pharmacist", "Pharmacist"],
  ["other_health_professional", "Other health professional"],
  ["lawyer", "Lawyer"],
  ["consumer", "Consumer or patient"],
);

export const ADMIN: FieldSpec[] = [
  { name: "report_type", label: "Report type", kind: enumOf(["spontaneous", "Spontaneous"],
      ["study", "Study"], ["other", "Other"], ["not_available", "Not available"]) }, // prettier-ignore
  { name: "initial_receipt_date", label: "Initial receipt date", kind: date },
  { name: "latest_receipt_date", label: "Latest receipt date", kind: date },
  { name: "occurrence_country", label: "Country of occurrence", kind: { type: "country" } },
];

export const PATIENT: FieldSpec[] = [
  { name: "initials", label: "Initials", kind: text },
  { name: "record_number", label: "Record number", kind: text },
  { name: "date_of_birth", label: "Date of birth", kind: date },
  { name: "age", label: "Age", kind: { type: "age" } },
  { name: "age_group", label: "Age group", kind: enumOf(["foetus", "Foetus"], ["neonate", "Neonate"],
      ["infant", "Infant"], ["child", "Child"], ["adolescent", "Adolescent"], ["adult", "Adult"],
      ["elderly", "Elderly"]) }, // prettier-ignore
  { name: "sex", label: "Sex", kind: SEX },
  { name: "weight_kg", label: "Weight (kg)", kind: { type: "number" } },
  { name: "height_cm", label: "Height (cm)", kind: { type: "number" } },
];

export const REPORTER: FieldSpec[] = [
  { name: "qualification", label: "Qualification", kind: QUALIFICATION },
  { name: "given_name", label: "Given name", kind: text },
  { name: "family_name", label: "Family name", kind: text },
  { name: "organisation", label: "Organisation", kind: text },
  { name: "email", label: "Email", kind: text },
  { name: "telephone", label: "Telephone", kind: text },
  { name: "country", label: "Country", kind: { type: "country" } },
];

export const DRUG: FieldSpec[] = [
  { name: "role", label: "Role", kind: enumOf(["suspect", "Suspect"], ["concomitant", "Concomitant"],
      ["interacting", "Interacting"], ["not_administered", "Not administered"]) }, // prettier-ignore
  { name: "product_name", label: "Product name (as reported)", kind: text },
  { name: "active_substance", label: "Active substance", kind: text },
  { name: "dose", label: "Dose", kind: { type: "dose" } },
  { name: "route", label: "Route", kind: text },
  { name: "start_date", label: "Start date", kind: date },
  { name: "end_date", label: "End date", kind: date },
  { name: "indication", label: "Indication", kind: text },
  { name: "action_taken", label: "Action taken", kind: enumOf(["withdrawn", "Withdrawn"],
      ["dose_reduced", "Dose reduced"], ["dose_increased", "Dose increased"],
      ["dose_not_changed", "Dose not changed"], ["unknown", "Unknown"],
      ["not_applicable", "Not applicable"]) }, // prettier-ignore
];

export const REACTION: FieldSpec[] = [
  { name: "reported_term", label: "Reported term", kind: text },
  { name: "meddra_code", label: "MedDRA LLT", kind: { type: "meddra" } },
  { name: "onset_date", label: "Onset date", kind: date },
  { name: "end_date", label: "End date", kind: date },
  { name: "outcome", label: "Outcome", kind: enumOf(["recovered", "Recovered"],
      ["recovering", "Recovering"], ["not_recovered", "Not recovered"],
      ["recovered_with_sequelae", "Recovered with sequelae"], ["fatal", "Fatal"],
      ["unknown", "Unknown"]) }, // prettier-ignore
  { name: "expectedness", label: "Expectedness", kind: enumOf(["expected", "Expected"],
      ["unexpected", "Unexpected"], ["unknown", "Unknown"]) }, // prettier-ignore
];

export const CRITERIA = [
  ["death", "Death"],
  ["life_threatening", "Life-threatening"],
  ["hospitalisation", "Hospitalisation"],
  ["disability", "Disability"],
  ["congenital_anomaly", "Congenital anomaly"],
  ["other_medically_important", "Other medically important"],
] as const;

export const NARRATIVE: FieldSpec[] = [
  { name: "case_narrative", label: "Case narrative", kind: { type: "text", multiline: true } },
  { name: "reporter_comments", label: "Reporter's comments", kind: { type: "text", multiline: true } },
  { name: "sender_comments", label: "Sender's comments", kind: { type: "text", multiline: true } },
];

export function isTracked(value: unknown): value is Tracked {
  return typeof value === "object" && value !== null && "source" in value && "value" in value;
}

/** Every tracked value under a node, with its path (as the API's change paths). */
export function trackedFields(node: unknown, path = ""): [string, Tracked][] {
  if (isTracked(node)) return [[path, node]];
  if (Array.isArray(node)) {
    return node.flatMap((item: Item) => trackedFields(item, `${path}/${item.id}`));
  }
  if (typeof node === "object" && node !== null) {
    return Object.entries(node).flatMap(([key, child]) =>
      key === "id" || key === "schema_version" ? [] : trackedFields(child, `${path}/${key}`),
    );
  }
  return [];
}

export function unconfirmedPaths(content: CaseContent, under = ""): string[] {
  return trackedFields(content)
    .filter(([path, field]) => path.startsWith(under) && !field.confirmed_by)
    .map(([path]) => path);
}

/** Display text for a stored value (ages and doses are objects). */
export function display(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") {
    const v = value as { value?: unknown; unit?: string };
    const unit = v.unit === "a" ? "years" : v.unit;
    return [v.value, unit].filter(Boolean).join(" ");
  }
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return String(value);
}

/** A form string to the value the API stores, or null to clear. */
export function parseInput(kind: FieldKind, raw: string, unit?: string): unknown {
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  if (kind.type === "age") return { value: trimmed, unit: unit || "a" };
  if (kind.type === "dose") return { value: trimmed, unit: unit || "mg" };
  if (kind.type === "country") return trimmed.toUpperCase();
  return trimmed;
}
