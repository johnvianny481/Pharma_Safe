/**
 * The case workspace: the source document with evidence highlighting next to the
 * editable E2B form, the AI panels, the task the user is working, and the history.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, Tab, TabList, TabPanel, Tabs } from "react-aria-components";
import { useParams } from "react-router";

import { apiPost, type EditOp, type Task } from "../api";
import { useToken } from "../auth";
import {
  AiSummary,
  DuplicatesPanel,
  FlagBadges,
  FollowUpPanel,
  NarrativePanel,
} from "../components/AiPanels";
import {
  CaseFormContext,
  ItemsSection,
  SeriousnessGrid,
  Section,
} from "../components/CaseForm";
import { ESignDialog } from "../components/ESignDialog";
import { AuditTrail, VersionHistory } from "../components/History";
import { SourcePanel, type Highlight } from "../components/SourcePanel";
import { Badge, Button, ErrorText, ModalDialog, TextField } from "../components/ui";
import { ADMIN, DRUG, isTracked, PATIENT, REACTION, REPORTER, trackedFields } from "../model";
import {
  useAiRuns,
  useAssessment,
  useCase,
  useEdit,
  useMe,
  useQueue,
  useSource,
  useVersion,
} from "../queries";
import { TASK_NAMES } from "./QueuePage";

const EDITORS = ["case_processor", "quality_reviewer", "medical_reviewer"];
const CREATORS = ["intake_processor", "case_processor"];
const MEANING: Record<Task["kind"], string> = {
  data_entry: "Reviewed",
  quality_review: "Quality reviewed",
  medical_review: "Medically approved",
  approval: "Approved",
};

function TaskBar(props: { task: Task | undefined; caseNumber: string; me: string | undefined }) {
  const token = useToken();
  const client = useQueryClient();
  const [signing, setSigning] = useState(false);
  const [returning, setReturning] = useState(false);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<unknown>(null);
  const { task } = props;
  if (!task) return null;
  const refresh = () => client.invalidateQueries();
  const mine = task.assignee === null || task.assignee === props.me;

  return (
    <section aria-label="Your task" className="taskbar">
      <p>
        Your task: <strong>{TASK_NAMES[task.kind]}</strong> of version {task.case_version}.
      </p>
      {task.assignee === null && (
        <Button onPress={() => void apiPost(`/tasks/${task.id}/claim`, {}, token).then(refresh).catch(setError)}>
          Claim task
        </Button>
      )}
      {mine && (
        <Button variant="primary" onPress={() => setSigning(true)}>
          Sign and complete: {MEANING[task.kind]}
        </Button>
      )}
      {mine && task.kind !== "data_entry" && (
        <Button variant="danger" onPress={() => setReturning(true)}>Return to data entry</Button>
      )}
      <ErrorText error={error} />
      <ESignDialog
        isOpen={signing}
        onOpenChange={setSigning}
        meaning={MEANING[task.kind]}
        subject={`${props.caseNumber}, version ${task.case_version}`}
        onSign={async (reauth, why) => {
          await apiPost(`/tasks/${task.id}/complete`, { reauth_token: reauth, reason: why || null }, token);
          await refresh();
        }}
      />
      <ModalDialog isOpen={returning} onOpenChange={setReturning} title="Return to data entry">
        <TextField label="Reason" value={reason} onChange={setReason} multiline isRequired />
        <div className="actions">
          <Button variant="quiet" onPress={() => setReturning(false)}>Cancel</Button>
          <Button
            variant="danger"
            isDisabled={reason.trim() === ""}
            onPress={() =>
              void apiPost(`/tasks/${task.id}/return`, { reason }, token)
                .then(() => { setReturning(false); return refresh(); })
                .catch(setError)
            }
          >
            Return
          </Button>
        </div>
      </ModalDialog>
    </section>
  );
} // prettier-ignore

export function CasePage() {
  const { caseId = "" } = useParams();
  const token = useToken();
  const client = useQueryClient();
  const me = useMe();
  const detail = useCase(caseId);
  const latest = detail.data?.latest_version;
  const version = useVersion(caseId, latest);
  const assessment = useAssessment(caseId, latest);
  const runs = useAiRuns(caseId, latest);
  const source = useSource(caseId);
  const queue = useQueue();
  const edit = useEdit(caseId, latest ?? 1);
  const [activeEvidence, setActiveEvidence] = useState<string | null>(null);
  // Kept here, not in the tabs: a new version (follow-up) reloads the page's data.
  const [tab, setTab] = useState<string>("case");

  const content = version.data?.content;
  const highlights = useMemo<Highlight[]>(() => {
    if (!content || !source.data) return [];
    return trackedFields(content)
      .filter(([, f]) => f.evidence && f.evidence.document_id === source.data?.document_id)
      .map(([path, f]) => ({ path, start: f.evidence!.start, end: f.evidence!.end }));
  }, [content, source.data]);

  if (detail.isError) return <ErrorText error={detail.error} />;
  if (!detail.data || !version.data || !content) return <p role="status">Loading the case…</p>;

  const roles = me.data?.roles ?? [];
  const merged = detail.data.merged_into;
  const editable = version.data.status === "draft" && !merged && roles.some((r) => EDITORS.includes(r));
  const pipeline = runs.data?.find((r) => r.kind === "pipeline");
  const narrativeRun = runs.data?.find((r) => r.kind === "narrative");
  const task = queue.data?.find((t) => t.case_id === caseId && t.case_version === latest);
  const narrative = content.narrative.case_narrative;
  const blockers = assessment.data?.approval_blockers ?? [];

  const save = async (changes: EditOp[]) => {
    await edit.mutateAsync({ revision: version.data.revision, changes });
    await client.invalidateQueries({ queryKey: ["case", caseId] });
  };

  return (
    <CaseFormContext.Provider
      value={{ content, editable, save, activeEvidence, showEvidence: setActiveEvidence }}
    >
      <header className="case-header">
        <h1>
          Case {detail.data.case_number}{" "}
          <span className="hint">version {version.data.version}</span>
        </h1>
        <p>
          <Badge tone={version.data.status === "approved" ? "ok" : "info"}>{version.data.status}</Badge>{" "}
          <Badge tone="muted">Stage: {detail.data.workflow_stage.replaceAll("_", " ")}</Badge>{" "}
          {assessment.data?.serious && <Badge tone="danger">Serious</Badge>}{" "}
          {pipeline && <FlagBadges flags={pipeline.flags} />}
        </p>
        {merged && (
          <p role="status" className="banner">
            This case was merged as a duplicate into <Link href={`/cases/${merged}`}>another case</Link>.
            It is closed.
          </p>
        )}
        <TaskBar task={task} caseNumber={detail.data.case_number} me={me.data?.subject} />
        <ErrorText error={edit.error} />
      </header>

      <Tabs className="tabs" selectedKey={tab} onSelectionChange={(key) => setTab(String(key))}>
        <TabList aria-label="Case views" className="tablist">
          <Tab id="case" className="tab">Case</Tab>
          <Tab id="history" className="tab">History and audit</Tab>
        </TabList>
        <TabPanel id="case" className="workspace">
          <div className="workspace-source">
            <SourcePanel source={source.data} highlights={highlights} active={activeEvidence} />
          </div>
          <div className="workspace-form">
            {blockers.length > 0 && (
              <details className="panel">
                <summary>{blockers.length} things to resolve before approval</summary>
                <ul>{blockers.map((b, i) => <li key={i}>{b.path}: {b.message}</li>)}</ul>
              </details>
            )}
            <AiSummary run={pipeline} />
            <DuplicatesPanel run={pipeline} caseId={caseId} caseNumber={detail.data.case_number}
                             content={content} canMerge={editable} />
            <Section title="Administrative" base="/admin" values={content.admin} specs={ADMIN} />
            <ItemsSection title="Reporters" section="reporters" noun="Reporter" specs={REPORTER} />
            <Section title="Patient" base="/patient" values={content.patient} specs={PATIENT} />
            <ItemsSection title="Drugs" section="drugs" noun="Drug" specs={DRUG} />
            <ItemsSection
              title="Reactions"
              section="reactions"
              noun="Reaction"
              specs={REACTION}
              extra={(item) => <SeriousnessGrid reaction={item} signals={pipeline?.result.seriousness} />}
            />
            <NarrativePanel
              key={String(narrative?.value ?? "")}
              draft={narrativeRun?.result.draft}
              current={isTracked(narrative) ? String(narrative.value) : ""}
              editable={editable}
              onSave={(text) => save([{ op: "set", path: "/narrative/case_narrative", value: text || null }])}
            />
            <FollowUpPanel run={pipeline} />
          </div>
        </TabPanel>
        <TabPanel id="history" className="history">
          <VersionHistory
            detail={detail.data}
            canStartFollowUp={roles.some((r) => CREATORS.includes(r)) && !merged}
            onStartFollowUp={async (receipt) => {
              await apiPost(`/cases/${caseId}/follow-ups`, { receipt_date: receipt }, token);
              await client.invalidateQueries({ queryKey: ["case", caseId] });
            }}
          />
          <AuditTrail caseId={caseId} />
        </TabPanel>
      </Tabs>
    </CaseFormContext.Provider>
  );
} // prettier-ignore
