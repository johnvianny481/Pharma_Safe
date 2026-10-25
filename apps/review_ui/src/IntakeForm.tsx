import { useMutation } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { apiPost, type IntakeAck } from "./api";

const QUALIFICATIONS = [
  ["", "Not given"],
  ["physician", "Physician"],
  ["pharmacist", "Pharmacist"],
  ["other_health_professional", "Other health professional"],
  ["lawyer", "Lawyer"],
  ["consumer", "Consumer or patient"],
] as const;

const SEXES = [
  ["", "Not given"],
  ["female", "Female"],
  ["male", "Male"],
  ["unknown", "Unknown"],
] as const;

type Field =
  | "given_name"
  | "family_name"
  | "email"
  | "telephone"
  | "qualification"
  | "initials"
  | "age"
  | "sex"
  | "product_name"
  | "reaction"
  | "onset_date"
  | "occurrence_country"
  | "description";

type Values = Record<Field, string>;

const EMPTY: Values = {
  given_name: "",
  family_name: "",
  email: "",
  telephone: "",
  qualification: "",
  initials: "",
  age: "",
  sex: "",
  product_name: "",
  reaction: "",
  onset_date: "",
  occurrence_country: "",
  description: "",
};

/** Empty strings become null, so the API records "not given" rather than an empty value. */
function orNull(value: string): string | null {
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

export function toPayload(v: Values) {
  return {
    reporter: {
      given_name: orNull(v.given_name),
      family_name: orNull(v.family_name),
      email: orNull(v.email),
      telephone: orNull(v.telephone),
      qualification: orNull(v.qualification),
    },
    patient: { initials: orNull(v.initials), age: orNull(v.age), sex: orNull(v.sex) },
    product_name: orNull(v.product_name),
    reaction: orNull(v.reaction),
    onset_date: orNull(v.onset_date),
    occurrence_country: orNull(v.occurrence_country.toUpperCase()),
    description: orNull(v.description),
  };
}

/**
 * Web and call-centre intake form. Every field is optional: a report is never refused.
 *
 * One idempotency key belongs to one report. Retrying after an error reuses it, so a
 * report sent twice (a double click, a lost response) still creates only one case.
 */
export function IntakeForm({ accessToken }: { accessToken: string }) {
  const [values, setValues] = useState<Values>(EMPTY);
  const [key, setKey] = useState(() => crypto.randomUUID());
  const submit = useMutation({
    mutationFn: (payload: ReturnType<typeof toPayload>) =>
      apiPost<IntakeAck>("/intake/forms", payload, accessToken, key),
  });

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    submit.mutate(toPayload(values));
  }

  function startNew() {
    setValues(EMPTY);
    setKey(crypto.randomUUID());
    submit.reset();
  }

  if (submit.isSuccess) {
    const numbers = submit.data.reports.map((r) => r.case_number).join(", ");
    return (
      <section aria-labelledby="intake-heading">
        <h2 id="intake-heading">Report received</h2>
        <p role="status">Created case {numbers}.</p>
        <button type="button" onClick={startNew}>
          Enter another report
        </button>
      </section>
    );
  }

  const input = (field: Field, label: string, props: Record<string, string> = {}) => (
    <label>
      {label}
      <input
        name={field}
        value={values[field]}
        onChange={(e) => setValues({ ...values, [field]: e.target.value })}
        {...props}
      />
    </label>
  );
  const select = (field: Field, label: string, options: readonly (readonly [string, string])[]) => (
    <label>
      {label}
      <select
        name={field}
        value={values[field]}
        onChange={(e) => setValues({ ...values, [field]: e.target.value })}
      >
        {options.map(([value, text]) => (
          <option key={value} value={value}>
            {text}
          </option>
        ))}
      </select>
    </label>
  );

  return (
    <section aria-labelledby="intake-heading">
      <h2 id="intake-heading">New adverse event report</h2>
      <form onSubmit={onSubmit}>
        <fieldset>
          <legend>Reporter</legend>
          {input("given_name", "Given name")}
          {input("family_name", "Family name")}
          {input("email", "Email", { type: "email" })}
          {input("telephone", "Telephone", { type: "tel" })}
          {select("qualification", "Qualification", QUALIFICATIONS)}
        </fieldset>
        <fieldset>
          <legend>Patient</legend>
          {input("initials", "Initials")}
          {input("age", "Age in years", { inputMode: "decimal" })}
          {select("sex", "Sex", SEXES)}
        </fieldset>
        <fieldset>
          <legend>What happened</legend>
          {input("product_name", "Suspected medicine")}
          {input("reaction", "Reaction")}
          {input("onset_date", "Onset date (YYYY, YYYY-MM or YYYY-MM-DD)", {
            pattern: "\\d{4}(-\\d{2}(-\\d{2})?)?",
          })}
          {input("occurrence_country", "Country (2 letters)", { maxLength: "2" })}
          <label>
            Description
            <textarea
              name="description"
              value={values.description}
              onChange={(e) => setValues({ ...values, description: e.target.value })}
            />
          </label>
        </fieldset>
        {submit.isError && (
          <p role="alert">
            The report was not saved ({submit.error.message}). Submit again: it will not be
            duplicated.
          </p>
        )}
        <button type="submit" disabled={submit.isPending}>
          {submit.isPending ? "Submitting…" : "Submit report"}
        </button>
      </form>
    </section>
  );
}
