/** Panels for what the AI pipeline proposed: summary, duplicates, follow-up, narrative. */
import { useMemo, useState } from "react";
import { useNavigate } from "react-router";

import { apiPost, type AiRun, type CaseContent, type DuplicateCandidate, type Item } from "../api";
import { useToken } from "../auth";
import { ADMIN, display, isTracked, PATIENT } from "../model";
import { useCase, useVersion } from "../queries";
import { ESignDialog } from "./ESignDialog";
import { Badge, Button, ModalDialog, TextField } from "./ui";

const FLAG_TEXT: Record<string, [string, "danger" | "warn" | "info"]> = {
  serious_by_signals: ["Serious (AI or rules)", "danger"],
  seriousness_disagreement: ["LLM and rules disagree on seriousness", "warn"],
  duplicate_candidates: ["Possible duplicate", "warn"],
  low_confidence: ["Low-confidence values", "warn"],
  rejected_values: ["Some AI values were rejected", "info"],
  ai_unavailable: ["AI unavailable: manual entry (L0)", "warn"],
  no_source_text: ["No source text", "info"],
  straight_through: ["Eligible for bulk QC sign-off (L2)", "info"],
};

export function FlagBadges({ flags }: { flags: string[] }) {
  return (
    <span className="flags">
      {flags.map((flag) => {
        const [text, tone] = FLAG_TEXT[flag] ?? [flag, "info"];
        return <Badge key={flag} tone={tone}>{text}</Badge>;
      })}
    </span>
  );
} // prettier-ignore

export function AiSummary({ run }: { run: AiRun | undefined }) {
  if (!run) return <p className="hint">The AI pipeline has not run for this version.</p>;
  const rejections = run.result.rejections ?? [];
  return (
    <section aria-labelledby="ai-heading" className="panel">
      <h2 id="ai-heading">AI processing</h2>
      <p>
        Automation level <strong>{run.automation_level}</strong>
        {run.result.automation?.reasons?.length ? ` (${run.result.automation.reasons.join("; ")})` : ""}.
        Configuration {run.bundle_version}.
      </p>
      <FlagBadges flags={run.flags} />
      {run.automation_level === "L0" && (run.result.proposals?.length ?? 0) > 0 && (
        <details>
          <summary>AI suggestions ({run.result.proposals?.length}): not entered in the case</summary>
          <ul>
            {run.result.proposals?.map((p) => (
              <li key={`${p.op}${p.path}`}>
                {p.path}: {display(p.value?.value ?? (p.item ? "new item" : ""))}
              </li>
            ))}
          </ul>
        </details>
      )}
      {rejections.length > 0 && (
        <details>
          <summary>{rejections.length} AI values rejected (no valid evidence)</summary>
          <ul>
            {rejections.map((r, i) => <li key={i}>{r.path}: {r.reason}</li>)}
          </ul>
        </details>
      )}
    </section>
  );
} // prettier-ignore

export function FollowUpPanel({ run }: { run: AiRun | undefined }) {
  const questions = run?.result.follow_up_questions ?? [];
  if (questions.length === 0) return null;
  return (
    <section aria-labelledby="follow-up-heading" className="panel">
      <h2 id="follow-up-heading">Follow-up questions</h2>
      <p className="hint">Suggested questions to the reporter, for information the case is missing.</p>
      <ol>
        {questions.map((q) => <li key={q.path}>{q.question}</li>)}
      </ol>
      <Button onPress={() => void navigator.clipboard?.writeText(questions.map((q) => q.question).join("\n"))}>
        Copy questions
      </Button>
    </section>
  );
} // prettier-ignore

export function NarrativePanel(props: {
  draft: string | undefined;
  current: string;
  editable: boolean;
  onSave: (text: string) => Promise<unknown>;
}) {
  const [text, setText] = useState(props.current);
  return (
    <section aria-labelledby="narrative-heading" className="panel">
      <h2 id="narrative-heading">Narrative</h2>
      {props.draft && (
        <div className="draft">
          <h3>AI draft (from confirmed values only)</h3>
          <p>{props.draft}</p>
          {props.editable && <Button onPress={() => setText(props.draft ?? "")}>Use the draft as a starting point</Button>}
        </div>
      )}
      <TextField label="Case narrative (H.1)" value={text} onChange={setText} multiline isReadOnly={!props.editable} />
      {props.editable && text !== props.current && (
        <Button variant="primary" onPress={() => void props.onSave(text)}>Save narrative</Button>
      )}
    </section>
  );
} // prettier-ignore

// ---------------------------------------------------------------- duplicates and merge

interface ComparisonRow {
  path: string;
  label: string;
  mine: string;
  theirs: string;
}

function itemLabel(item: Item): string {
  const name = ["reported_term", "product_name", "family_name", "qualification", "test_name"]
    .map((k) => item[k])
    .find(isTracked);
  return name ? display(name.value) : item.id;
}

/** Fields and items of this case, next to the master's, for picking what to keep. */
export function comparisonRows(mine: CaseContent, theirs: CaseContent | undefined): ComparisonRow[] {
  const rows: ComparisonRow[] = [];
  for (const [section, specs] of [["admin", ADMIN], ["patient", PATIENT]] as const) {
    for (const spec of specs) {
      const value = mine[section][spec.name];
      if (!isTracked(value)) continue;
      const other = theirs?.[section][spec.name];
      rows.push({
        path: `/${section}/${spec.name}`,
        label: spec.label,
        mine: display(value.value),
        theirs: isTracked(other) ? display(other.value) : "",
      });
    }
  }
  for (const [section, noun] of [["reporters", "Reporter"], ["drugs", "Drug"], ["reactions", "Reaction"]] as const) {
    for (const item of mine[section]) {
      rows.push({
        path: `/${section}/${item.id}`,
        label: `${noun}: ${itemLabel(item)}`,
        mine: "whole item",
        theirs: (theirs?.[section] ?? []).map(itemLabel).join(", "),
      });
    }
  }
  return rows;
} // prettier-ignore

function MergeDialog(props: {
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  caseId: string;
  caseNumber: string;
  content: CaseContent;
  candidate: DuplicateCandidate;
}) {
  const token = useToken();
  const navigate = useNavigate();
  const master = useCase(props.candidate.case_id);
  const masterVersion = useVersion(props.candidate.case_id, master.data?.latest_version);
  const rows = useMemo(
    () => comparisonRows(props.content, masterVersion.data?.content),
    [props.content, masterVersion.data],
  );
  const [picks, setPicks] = useState<Set<string>>(new Set());
  const [signing, setSigning] = useState(false);

  return (
    <>
      <ModalDialog isOpen={props.isOpen && !signing} onOpenChange={props.onOpenChange}
                   title={`Compare with ${props.candidate.case_number}`} wide>
        <p>
          Score {props.candidate.score.toFixed(2)}: {props.candidate.reasons.join("; ")}.
        </p>
        <p className="hint">
          Pick what to copy from {props.caseNumber} into {props.candidate.case_number}. Copied values
          arrive unconfirmed, with their evidence. {props.caseNumber} is then closed as a duplicate.
        </p>
        <table className="table">
          <caption className="sr-only">Comparison of the two cases</caption>
          <thead>
            <tr>
              <th scope="col">Field</th>
              <th scope="col">{props.caseNumber} (this case)</th>
              <th scope="col">{props.candidate.case_number}</th>
              <th scope="col">Copy</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.path}>
                <th scope="row">{row.label}</th>
                <td>{row.mine}</td>
                <td>{row.theirs || "—"}</td>
                <td>
                  <input
                    type="checkbox"
                    className="native-check"
                    checked={picks.has(row.path)}
                    onChange={(e) => {
                      const next = new Set(picks);
                      if (e.target.checked) next.add(row.path); else next.delete(row.path);
                      setPicks(next);
                    }}
                    aria-label={`Copy ${row.label} into ${props.candidate.case_number}`}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="actions">
          <Button variant="quiet" onPress={() => props.onOpenChange(false)}>Cancel</Button>
          <Button variant="primary" onPress={() => setSigning(true)}>
            Merge into {props.candidate.case_number}
          </Button>
        </div>
      </ModalDialog>
      <ESignDialog
        isOpen={signing}
        onOpenChange={(open) => { setSigning(open); if (!open) props.onOpenChange(false); }}
        meaning="Duplicate merged"
        subject={`${props.caseNumber} as a duplicate of ${props.candidate.case_number}`}
        onSign={async (reauth, reason) => {
          await apiPost(`/cases/${props.caseId}/merge`, {
            into: props.candidate.case_id, picks: [...picks], reauth_token: reauth,
            reason: reason || null,
          }, token);
          navigate(`/cases/${props.candidate.case_id}`);
        }}
      />
    </>
  );
} // prettier-ignore

export function DuplicatesPanel(props: {
  run: AiRun | undefined;
  caseId: string;
  caseNumber: string;
  content: CaseContent | undefined;
  canMerge: boolean;
}) {
  const [open, setOpen] = useState<DuplicateCandidate | null>(null);
  const candidates = props.run?.result.duplicates ?? [];
  if (candidates.length === 0) return null;
  return (
    <section aria-labelledby="dup-heading" className="panel">
      <h2 id="dup-heading">Possible duplicates</h2>
      <ul>
        {candidates.map((c) => (
          <li key={c.case_id}>
            <strong>{c.case_number}</strong> (score {c.score.toFixed(2)}): {c.reasons.join("; ")}{" "}
            {props.canMerge && props.content && (
              <Button onPress={() => setOpen(c)}>Compare with {c.case_number}</Button>
            )}
          </li>
        ))}
      </ul>
      {open && props.content && (
        <MergeDialog isOpen onOpenChange={(o) => !o && setOpen(null)} caseId={props.caseId}
                     caseNumber={props.caseNumber} content={props.content} candidate={open} />
      )}
    </section>
  );
} // prettier-ignore
