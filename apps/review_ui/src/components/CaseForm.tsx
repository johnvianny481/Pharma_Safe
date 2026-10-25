/**
 * The editable, E2B-structured case form.
 *
 * Every field shows where its value came from (AI, a person, an import), the AI's
 * confidence, and whether a person has confirmed it. Confirming keeps the value and
 * records who confirmed it; editing replaces it with a person's value. Low-confidence
 * AI values are flagged so the reviewer looks twice. Approval needs every value
 * confirmed (checked by the API, shown here as blockers).
 */
import { createContext, useContext, useState } from "react";
import {
  ComboBox,
  Input,
  Label,
  ListBox,
  ListBoxItem,
  Popover,
  Radio,
  RadioGroup,
} from "react-aria-components";

import type { CaseContent, EditOp, Item, Tracked } from "../api";
import { AuthContext, useToken } from "../auth";
import {
  CRITERIA,
  display,
  type FieldSpec,
  isTracked,
  parseInput,
  trackedFields,
} from "../model";
import { searchCodes } from "../queries";
import { Badge, Button, SelectField, TextField } from "./ui";

export const LOW_CONFIDENCE = 0.7;

export interface FormContext {
  content: CaseContent;
  editable: boolean;
  save: (changes: EditOp[]) => Promise<unknown>;
  showEvidence: (path: string) => void;
  activeEvidence: string | null;
}

export const CaseFormContext = createContext<FormContext | null>(null);

function useForm(): FormContext {
  const ctx = useContext(CaseFormContext);
  if (!ctx) throw new Error("CaseFormContext missing");
  return ctx;
}

export function Provenance({ field }: { field: Tracked }) {
  // Values record who confirmed them by subject id; names are in the audit trail.
  const me = useContext(AuthContext)?.user?.profile.sub;
  const source = { ai: "AI", human: "Person", import: "Import" }[field.source];
  const low = field.confidence !== null && field.confidence < LOW_CONFIDENCE;
  return (
    <span className="provenance">
      <Badge tone={field.source === "ai" ? "info" : "muted"}>{source}</Badge>
      {field.confidence !== null && (
        <Badge tone={low ? "warn" : "ok"} label={`Confidence ${Math.round(field.confidence * 100)} percent${low ? ", low" : ""}`}>
          {Math.round(field.confidence * 100)}%{low ? " low" : ""}
        </Badge>
      )}
      {field.confirmed_by ? (
        <Badge tone="ok">{field.confirmed_by === me ? "Confirmed by you" : "Confirmed"}</Badge>
      ) : (
        <Badge tone="warn">Unconfirmed</Badge>
      )}
    </span>
  );
} // prettier-ignore

function FieldEditor(props: { spec: FieldSpec; path: string; field: Tracked | null }) {
  const { editable, save } = useForm();
  const current = props.field?.value;
  const initial =
    typeof current === "object" && current !== null
      ? String((current as { value: unknown }).value ?? "")
      : display(current);
  const initialUnit =
    typeof current === "object" && current !== null
      ? String((current as { unit?: string }).unit ?? "")
      : "";
  const [draft, setDraft] = useState(initial);
  const [unit, setUnit] = useState(initialUnit);
  const kind = props.spec.kind;
  const changed = draft !== initial || unit !== initialUnit;

  if (kind.type === "enum") {
    return (
      <SelectField
        label={props.spec.label}
        value={typeof current === "string" ? current : null}
        options={kind.options}
        isDisabled={!editable}
        onChange={(value) => void save([{ op: "set", path: props.path, value }])}
      />
    );
  }
  if (kind.type === "meddra") {
    return <CodingPicker label={props.spec.label} path={props.path} value={current} />;
  }
  return (
    <div className="editor">
      <TextField
        label={props.spec.label}
        value={draft}
        onChange={setDraft}
        isReadOnly={!editable}
        multiline={kind.type === "text" && kind.multiline}
        inputMode={kind.type === "number" || kind.type === "age" || kind.type === "dose" ? "decimal" : undefined}
        description={kind.type === "date" ? "YYYY, YYYY-MM or YYYY-MM-DD" : undefined}
      />
      {(kind.type === "age" || kind.type === "dose") && (
        <TextField
          label={`${props.spec.label} unit`}
          value={unit}
          onChange={setUnit}
          isReadOnly={!editable}
          description={kind.type === "age" ? "a = years, mo, wk, d, h" : "UCUM, e.g. mg"}
        />
      )}
      {editable && changed && (
        <Button
          variant="primary"
          onPress={() =>
            void save([{ op: "set", path: props.path, value: parseInput(kind, draft, unit) }])
          }
        >
          Save {props.spec.label.toLowerCase()}
        </Button>
      )}
    </div>
  );
} // prettier-ignore

export function FieldRow({ spec, path, field }: {
  spec: FieldSpec;
  path: string;
  field: Tracked | null;
}) {
  const { editable, save, showEvidence, activeEvidence } = useForm();
  const key = `${path}:${JSON.stringify(field?.value ?? null)}`;
  return (
    <div className={`field-row ${activeEvidence === path ? "field-row-active" : ""}`} data-path={path}>
      <FieldEditor key={key} spec={spec} path={path} field={field} />
      {field && (
        <div className="field-meta">
          <Provenance field={field} />
          {field.evidence && (
            <Button variant="quiet" aria-label={`Show evidence for ${spec.label}`} onPress={() => showEvidence(path)}>
              Show evidence
            </Button>
          )}
          {editable && !field.confirmed_by && (
            <Button aria-label={`Confirm ${spec.label}`} onPress={() => void save([{ op: "confirm", path }])}>
              Confirm
            </Button>
          )}
        </div>
      )}
    </div>
  );
} // prettier-ignore

export function Section(props: {
  title: string;
  base: string;
  values: Record<string, unknown>;
  specs: FieldSpec[];
}) {
  const { editable, save, content } = useForm();
  const open = trackedFields(props.values, props.base).filter(([, f]) => !f.confirmed_by);
  return (
    <fieldset className="section">
      <legend>{props.title}</legend>
      {props.specs.map((spec) => {
        const value = props.values[spec.name];
        return (
          <FieldRow
            key={spec.name}
            spec={spec}
            path={`${props.base}/${spec.name}`}
            field={isTracked(value) ? value : null}
          />
        );
      })}
      {editable && open.length > 0 && (
        <Button onPress={() => void save(open.map(([path]) => ({ op: "confirm", path })))}>
          Confirm all {open.length} in {props.title.toLowerCase()}
        </Button>
      )}
      {content && null}
    </fieldset>
  );
}

export function ItemsSection(props: {
  title: string;
  section: "reporters" | "drugs" | "reactions";
  noun: string;
  specs: FieldSpec[];
  extra?: (item: Item) => React.ReactNode;
}) {
  const { content, editable, save } = useForm();
  const items = content[props.section];
  const addItem = () => {
    const taken = new Set(items.map((i) => i.id));
    let n = items.length + 1;
    while (taken.has(`${props.section[0]}${n}`)) n++;
    void save([{ op: "add", path: `/${props.section}`, id: `${props.section[0]}${n}` }]);
  };
  return (
    <section className="items" aria-label={props.title}>
      <h3>{props.title}</h3>
      {items.length === 0 && <p className="hint">None recorded.</p>}
      {items.map((item, index) => (
        <div key={item.id} className="item">
          <Section
            title={`${props.noun} ${index + 1}`}
            base={`/${props.section}/${item.id}`}
            values={item}
            specs={props.specs}
          />
          {props.extra?.(item)}
          {editable && (
            <Button variant="danger" onPress={() => void save([{ op: "remove", path: `/${props.section}/${item.id}` }])}>
              Remove {props.noun.toLowerCase()} {index + 1}
            </Button>
          )}
        </div>
      ))}
      {editable && <Button onPress={addItem}>Add {props.noun.toLowerCase()}</Button>}
    </section>
  );
} // prettier-ignore

/** Per-reaction ICH E2A criteria: each is assessed Yes or No by a person, or left open. */
export function SeriousnessGrid({ reaction, signals }: {
  reaction: Item;
  signals?: { llm: Record<string, boolean>; rules: Record<string, boolean> };
}) {
  const { editable, save } = useForm();
  const seriousness = (reaction.seriousness ?? {}) as Record<string, Tracked<boolean> | null>;
  const term = isTracked(reaction.reported_term) ? display(reaction.reported_term.value) : reaction.id;
  return (
    <fieldset className="seriousness">
      <legend>Seriousness of {term}</legend>
      {CRITERIA.map(([name, label]) => {
        const field = seriousness[name] ?? null;
        const path = `/reactions/${reaction.id}/seriousness/${name}`;
        const value = field === null ? "open" : field.value ? "yes" : "no";
        const llm = signals?.llm[name];
        const rules = signals?.rules[name];
        return (
          <div key={name} className="criterion">
            <RadioGroup
              className="radio-group"
              orientation="horizontal"
              value={value}
              isDisabled={!editable}
              onChange={(v) => void save([{ op: "set", path, value: v === "open" ? null : v === "yes" }])}
            >
              <Label>{label}</Label>
              <Radio value="yes">Yes</Radio>
              <Radio value="no">No</Radio>
              <Radio value="open">Not assessed</Radio>
            </RadioGroup>
            {signals && (
              <span className="signals">
                <Badge tone={llm ? "danger" : "muted"}>LLM: {llm ? "met" : "not met"}</Badge>
                <Badge tone={rules ? "danger" : "muted"}>Rules: {rules ? "met" : "not met"}</Badge>
                {Boolean(llm) !== Boolean(rules) && <Badge tone="warn">Signals disagree</Badge>}
              </span>
            )}
            {field && <Provenance field={field} />}
            {field && editable && !field.confirmed_by && (
              <Button aria-label={`Confirm ${label} for ${term}`} onPress={() => void save([{ op: "confirm", path }])}>
                Confirm
              </Button>
            )}
          </div>
        );
      })}
    </fieldset>
  );
} // prettier-ignore

/** MedDRA coding by search: the reviewer picks a dictionary term; codes are never typed. */
function CodingPicker(props: { label: string; path: string; value: unknown }) {
  const { editable, save } = useForm();
  const token = useToken();
  const [items, setItems] = useState<{ id: string; label: string }[]>([]);
  const [input, setInput] = useState("");
  const current = typeof props.value === "string" ? props.value : null;

  async function onInput(term: string) {
    setInput(term);
    if (term.trim().length < 2) return setItems([]);
    const result = await searchCodes(token, "meddra_llt", term);
    setItems(result.candidates.map((c) => ({ id: c.code, label: `${c.label} (${c.code})` })));
  }

  return (
    <ComboBox
      className="field"
      items={items}
      inputValue={input}
      onInputChange={(term) => void onInput(term)}
      onSelectionChange={(key) => key !== null && void save([{ op: "set", path: props.path, value: String(key) }])}
      isDisabled={!editable}
      menuTrigger="input"
    >
      <Label>{props.label}{current ? `: ${current}` : ""}</Label>
      <Input placeholder="Search the dictionary" />
      <Popover className="popover">
        <ListBox className="listbox">
          {(item: { id: string; label: string }) => (
            <ListBoxItem id={item.id} className="option">{item.label}</ListBoxItem>
          )}
        </ListBox>
      </Popover>
    </ComboBox>
  );
} // prettier-ignore
