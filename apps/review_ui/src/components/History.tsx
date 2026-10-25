/** Case version history (approved versions are read-only) and the case's audit trail. */
import { useState } from "react";
import { Cell, Column, Row, Table, TableBody, TableHeader } from "react-aria-components";

import type { CaseDetail } from "../api";
import { display, trackedFields } from "../model";
import { useCaseAudit, useVersion } from "../queries";
import { Badge, Button, ErrorText, TextField } from "./ui";

function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "";
}

export function VersionHistory(props: {
  detail: CaseDetail;
  canStartFollowUp: boolean;
  onStartFollowUp: (receiptDate: string) => Promise<unknown>;
}) {
  const [viewing, setViewing] = useState<number | null>(null);
  const [receipt, setReceipt] = useState(new Date().toISOString().slice(0, 10));
  const [error, setError] = useState<unknown>(null);
  const latest = props.detail.versions.at(-1);
  const shown = useVersion(props.detail.id, viewing ?? undefined);

  return (
    <section aria-labelledby="versions-heading" className="panel">
      <h2 id="versions-heading">Versions</h2>
      <Table aria-label="Case versions" className="table">
        <TableHeader>
          <Column isRowHeader>Version</Column>
          <Column>Status</Column>
          <Column>Created</Column>
          <Column>Approved</Column>
          <Column>View</Column>
        </TableHeader>
        <TableBody items={props.detail.versions.map((v) => ({ ...v, id: v.version }))}>
          {(v) => (
            <Row>
              <Cell>Version {v.version}</Cell>
              <Cell>
                <Badge tone={v.status === "approved" ? "ok" : "info"}>{v.status}</Badge>
              </Cell>
              <Cell>{when(v.created_at)} by {v.created_by}</Cell>
              <Cell>{v.approved_at ? `${when(v.approved_at)} by ${v.approved_by}` : "—"}</Cell>
              <Cell>
                <Button variant="quiet" onPress={() => setViewing(v.version)}>
                  View version {v.version}
                </Button>
              </Cell>
            </Row>
          )}
        </TableBody>
      </Table>
      {props.canStartFollowUp && latest?.status === "approved" && (
        <div className="follow-up">
          <h3>New information</h3>
          <p className="hint">
            Approved versions never change. New information starts a follow-up version; its
            receipt date can start new regulatory clocks.
          </p>
          <TextField label="Follow-up receipt date" type="date" value={receipt} onChange={setReceipt} />
          <Button variant="primary" onPress={() => void props.onStartFollowUp(receipt).catch(setError)}>
            Start follow-up version
          </Button>
          <ErrorText error={error} />
        </div>
      )}
      {viewing !== null && shown.data && (
        <section aria-label={`Version ${viewing} content`} className="readonly">
          <h3>
            Version {viewing} ({shown.data.status}), read-only
          </h3>
          <dl>
            {trackedFields(shown.data.content).map(([path, field]) => (
              <div key={path}>
                <dt>{path}</dt>
                <dd>
                  {display(field.value)} <span className="hint">({field.source}
                  {field.confirmed_by ? `, confirmed by ${field.confirmed_by}` : ", unconfirmed"})</span>
                </dd>
              </div>
            ))}
          </dl>
        </section>
      )}
    </section>
  );
} // prettier-ignore

function brief(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object" && "value" in (value as object)) {
    return display((value as { value: unknown }).value);
  }
  const text = JSON.stringify(value);
  return text.length > 80 ? `${text.slice(0, 77)}…` : text;
}

export function AuditTrail({ caseId }: { caseId: string }) {
  const audit = useCaseAudit(caseId);
  if (audit.isPending) return <p role="status">Loading the audit trail…</p>;
  if (audit.isError) return <ErrorText error={audit.error} />;
  return (
    <section aria-labelledby="audit-heading" className="panel">
      <h2 id="audit-heading">Audit trail</h2>
      <p className="hint">Tamper-evident: every entry is chained to the one before it.</p>
      <Table aria-label="Audit trail, newest first" className="table">
        <TableHeader>
          <Column isRowHeader>When</Column>
          <Column>Who</Column>
          <Column>What</Column>
          <Column>Field</Column>
          <Column>Old</Column>
          <Column>New</Column>
          <Column>Reason</Column>
        </TableHeader>
        <TableBody items={audit.data.map((e) => ({ ...e, id: e.seq }))}>
          {(e) => (
            <Row>
              <Cell>{when(e.occurred_at)}</Cell>
              <Cell>{e.actor_name}</Cell>
              <Cell>{e.action}{e.case_version ? ` (v${e.case_version})` : ""}</Cell>
              <Cell>{e.field ?? ""}</Cell>
              <Cell>{brief(e.old_value)}</Cell>
              <Cell>{brief(e.new_value)}</Cell>
              <Cell>{e.reason ?? ""}</Cell>
            </Row>
          )}
        </TableBody>
      </Table>
    </section>
  );
} // prettier-ignore
