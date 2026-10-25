/** The work queue: open tasks the user can work, most urgent regulatory deadline first. */
import { useQueryClient } from "@tanstack/react-query";
import { Cell, Column, Link, Row, Table, TableBody, TableHeader } from "react-aria-components";

import { apiPost, type Task } from "../api";
import { useToken } from "../auth";
import { FlagBadges } from "../components/AiPanels";
import { Badge, Button, ErrorText } from "../components/ui";
import { useMe, useQueue } from "../queries";

export const TASK_NAMES: Record<Task["kind"], string> = {
  data_entry: "Data entry review",
  quality_review: "Quality review",
  medical_review: "Medical review",
  approval: "Approval",
};

/** Days left to the nearest regulatory due date, as a badge. */
export function DeadlineRisk({ due, today = new Date() }: { due: string | null; today?: Date }) {
  if (!due) return <Badge tone="muted">No deadline</Badge>;
  const start = Date.UTC(today.getFullYear(), today.getMonth(), today.getDate());
  const days = Math.round((Date.parse(`${due}T00:00:00Z`) - start) / 86_400_000);
  if (days < 0) return <Badge tone="danger">Overdue by {-days} d (due {due})</Badge>;
  const tone = days <= 2 ? "danger" : days <= 5 ? "warn" : "ok";
  return <Badge tone={tone}>Due in {days} d ({due})</Badge>;
}

export function QueuePage() {
  const token = useToken();
  const me = useMe();
  const queue = useQueue();
  const client = useQueryClient();

  if (queue.isPending) return <p role="status">Loading your queue…</p>;
  if (queue.isError) return <ErrorText error={queue.error} />;
  const claim = async (task: Task) => {
    await apiPost(`/tasks/${task.id}/claim`, {}, token);
    await client.invalidateQueries({ queryKey: ["tasks"] });
  };
  return (
    <section aria-labelledby="queue-heading">
      <h1 id="queue-heading">Work queue</h1>
      <p className="hint">
        Open tasks for your roles and data scope, most urgent regulatory deadline first.
      </p>
      {queue.data.length === 0 ? (
        <p>Nothing to do right now.</p>
      ) : (
        <Table aria-label="Open tasks, most urgent first" className="table">
          <TableHeader>
            <Column isRowHeader>Case</Column>
            <Column>Task</Column>
            <Column>Deadline</Column>
            <Column>AI flags</Column>
            <Column>Assigned</Column>
          </TableHeader>
          <TableBody items={queue.data}>
            {(task) => (
              <Row id={task.id}>
                <Cell>
                  <Link href={`/cases/${task.case_id}`}>{task.case_number ?? task.case_id}</Link>
                  <span className="hint"> v{task.case_version}</span>
                </Cell>
                <Cell>{TASK_NAMES[task.kind]}</Cell>
                <Cell><DeadlineRisk due={task.next_due} /></Cell>
                <Cell><FlagBadges flags={task.flags} /></Cell>
                <Cell>
                  {task.assignee === me.data?.subject ? (
                    "You"
                  ) : task.assignee ? (
                    task.assignee
                  ) : (
                    <Button aria-label={`Claim ${task.case_number ?? task.case_id}`} onPress={() => void claim(task)}>
                      Claim
                    </Button>
                  )}
                </Cell>
              </Row>
            )}
          </TableBody>
        </Table>
      )}
    </section>
  );
} // prettier-ignore
