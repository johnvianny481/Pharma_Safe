"""The AI pipeline as a Temporal workflow, run as a child of the case workflow.

One run per case version (``ai-{case_id}-v{version}``) on the ``ai-pipeline`` task queue,
so AI workers scale separately from the case workflow. Each model call is its own
activity with its own retries.

**Graceful degradation.** If any model call reports that no provider is available (both
providers down, or the tenant's budget spent), or the gateway stays unreachable after
retries, the pipeline stops calling models and ``finalize`` records the case in **L0
manual mode**: people enter the data, while the rule-based steps (seriousness rules,
duplicate search) still run. The case workflow carries on either way; intake and
clocks never wait for the AI.
"""

import json
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from pharmasafe_ai_worker.contracts import (
        PIPELINE_WORKFLOW,
        FinalizeInput,
        Outputs,
        PipelineInput,
        PipelineSummary,
        Plan,
        PlanInput,
        Prepared,
        StepInput,
    )

MODEL_CALL_TIMEOUT = timedelta(minutes=10)
LOCAL_TIMEOUT = timedelta(minutes=2)
# The gateway already retries providers; these retries cover an unreachable gateway.
MODEL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
    non_retryable_error_types=["AiUnavailable"],
)
LOCAL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=10,
    non_retryable_error_types=["BundleRefused"],
)


@workflow.defn(name=PIPELINE_WORKFLOW)
class CasePipelineWorkflow:
    @workflow.run
    async def run(self, inp: PipelineInput) -> PipelineSummary:
        prepared: Prepared = await workflow.execute_activity(
            "prepare", inp, result_type=Prepared,
            start_to_close_timeout=LOCAL_TIMEOUT, retry_policy=LOCAL_RETRY,
        )  # fmt: skip
        outputs = Outputs()
        available, reason = True, None
        text = prepared.redacted_text
        if text:
            try:
                await self._model_calls(inp, prepared, text, outputs)
            except ActivityError as exc:
                available, reason = False, _reason(exc)
                outputs = Outputs()  # nothing partial reaches the case
        summary: PipelineSummary = await workflow.execute_activity(
            "finalize",
            FinalizeInput(
                inp.tenant_id,
                inp.case_id,
                inp.version,
                prepared.bundle_version,
                outputs,
                ai_available=available,
                unavailable_reason=reason,
            ),
            result_type=PipelineSummary,
            start_to_close_timeout=LOCAL_TIMEOUT,
            retry_policy=LOCAL_RETRY,
        )
        return summary

    async def _model_calls(
        self, inp: PipelineInput, prepared: Prepared, text: str, outputs: Outputs
    ) -> None:
        def step(variables: dict[str, str]) -> StepInput:
            return StepInput(
                inp.tenant_id, inp.case_id, inp.version, prepared.bundle_version, variables
            )

        outputs.extraction = await self._model("extract", step({"report": text}))
        plan: Plan = await workflow.execute_activity(
            "plan",
            PlanInput(inp.tenant_id, inp.case_id, inp.version, prepared, outputs.extraction),
            result_type=Plan,
            start_to_close_timeout=LOCAL_TIMEOUT,
            retry_policy=LOCAL_RETRY,
        )
        if plan.reactions:
            outputs.seriousness = await self._model(
                "assess_seriousness",
                step({"report": text, "reactions": json.dumps(plan.reactions)}),
            )
        if plan.coding_items:
            outputs.coding = await self._model(
                "code_terms", step({"items": json.dumps(plan.coding_items)})
            )
        if plan.gaps:
            outputs.follow_up = await self._model(
                "ask_follow_up", step({"gaps": json.dumps(plan.gaps)})
            )

    async def _model(self, name: str, arg: StepInput) -> dict[str, object]:
        result: dict[str, object] = await workflow.execute_activity(
            name, arg, result_type=dict,
            start_to_close_timeout=MODEL_CALL_TIMEOUT, retry_policy=MODEL_RETRY,
        )  # fmt: skip
        return result


def _reason(exc: ActivityError) -> str:
    cause = exc.cause
    return str(cause.message if cause is not None and hasattr(cause, "message") else exc)[:500]
