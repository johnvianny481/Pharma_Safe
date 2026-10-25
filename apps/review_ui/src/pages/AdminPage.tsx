/** Tenant administration: settings (audited) and audit-chain verification. */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Checkbox, CheckboxGroup, Label } from "react-aria-components";

import { apiPost, apiRequest, type TenantSettings } from "../api";
import { useToken } from "../auth";
import { Badge, Button, ErrorText, SelectField, TextField } from "../components/ui";
import { useTenantSettings } from "../queries";

const LEVELS = [
  ["L0", "L0: AI suggests, people enter all data"],
  ["L1", "L1: AI pre-fills, people confirm every value"],
  ["L2", "L2: straight-through for non-serious, valid, high-confidence cases"],
] as const;

const ROLES: [string, string][] = [
  ["intake_processor", "Enters reports from any channel"],
  ["case_processor", "Data entry review; signs “Reviewed”"],
  ["quality_reviewer", "Quality review and approval"],
  ["medical_reviewer", "Medical review of serious cases and approval"],
  ["tenant_admin", "These settings and audit verification"],
];

interface ChainStatus {
  ok: boolean;
  entries_checked: number;
  first_invalid_seq: number | null;
  problem: string | null;
}

function SettingsForm({ settings }: { settings: TenantSettings }) {
  const token = useToken();
  const client = useQueryClient();
  const [level, setLevel] = useState<string | null>(settings.ai_automation_level);
  const [qc, setQc] = useState(settings.qc_sample_rate);
  const [budget, setBudget] = useState(settings.ai_monthly_budget_usd);
  const [mailbox, setMailbox] = useState(settings.intake_mailbox ?? "");
  const [regulators, setRegulators] = useState<string[]>(settings.regulators);
  const [reason, setReason] = useState("");
  const save = useMutation({
    mutationFn: () =>
      apiRequest<TenantSettings>("PATCH", "/admin/tenant", token, {
        ai_automation_level: level,
        qc_sample_rate: qc,
        ai_monthly_budget_usd: budget,
        intake_mailbox: mailbox.trim() || null,
        regulators,
        reason: reason.trim() || null,
      }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["admin"] }),
  });

  return (
    <form
      className="panel"
      aria-labelledby="settings-heading"
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
    >
      <h2 id="settings-heading">Settings for {settings.name}</h2>
      <p className="hint">Region {settings.region}. Every change is recorded in the audit trail.</p>
      <SelectField label="AI automation level" value={level} options={LEVELS} onChange={setLevel} />
      <p className="hint">Serious cases never go beyond L1, whatever this says.</p>
      <TextField label="QC sampling rate for non-serious cases (0 to 1)" value={qc} onChange={setQc} inputMode="decimal" />
      <TextField label="Monthly AI budget (USD)" value={budget} onChange={setBudget} inputMode="decimal" />
      <TextField label="Intake mailbox" value={mailbox} onChange={setMailbox} type="email" />
      <CheckboxGroup className="field" value={regulators} onChange={setRegulators}>
        <Label>Regulators whose clock rules apply</Label>
        {settings.available_regulators.map((r) => (
          <Checkbox key={r} value={r} className="check">
            <span className="checkbox" aria-hidden="true" />
            {r}
          </Checkbox>
        ))}
      </CheckboxGroup>
      <TextField label="Reason for the change" value={reason} onChange={setReason} />
      <ErrorText error={save.error} />
      {save.isSuccess && <p role="status">Settings saved.</p>}
      <Button type="submit" variant="primary" isDisabled={save.isPending}>Save settings</Button>
    </form>
  );
} // prettier-ignore

export function AdminPage() {
  const token = useToken();
  const settings = useTenantSettings();
  const verify = useMutation({ mutationFn: () => apiPost<ChainStatus>("/audit/verify", {}, token) });

  if (settings.isPending) return <p role="status">Loading tenant settings…</p>;
  if (settings.isError) return <ErrorText error={settings.error} />;
  return (
    <section aria-labelledby="admin-heading">
      <h1 id="admin-heading">Tenant administration</h1>
      <SettingsForm key={settings.data.slug} settings={settings.data} />
      <section className="panel" aria-labelledby="audit-verify-heading">
        <h2 id="audit-verify-heading">Audit trail integrity</h2>
        <p className="hint">Checks every entry's hash against the one before it.</p>
        <Button onPress={() => verify.mutate()} isDisabled={verify.isPending}>Verify the audit chain</Button>
        {verify.data && (
          <p role="status">
            {verify.data.ok ? (
              <Badge tone="ok">Intact: {verify.data.entries_checked} entries checked</Badge>
            ) : (
              <Badge tone="danger">Broken at entry {verify.data.first_invalid_seq}: {verify.data.problem}</Badge>
            )}
          </p>
        )}
        <ErrorText error={verify.error} />
      </section>
      <section className="panel" aria-labelledby="roles-heading">
        <h2 id="roles-heading">Roles</h2>
        <p className="hint">
          Users, roles and data scopes are managed in your identity provider and shown here for reference.
        </p>
        <dl>
          {ROLES.map(([role, text]) => (
            <div key={role}><dt>{role}</dt><dd>{text}</dd></div>
          ))}
        </dl>
      </section>
    </section>
  );
} // prettier-ignore
