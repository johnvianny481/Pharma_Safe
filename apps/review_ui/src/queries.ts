/** TanStack Query hooks over the API. Mutations refresh what they change. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  apiGet,
  apiRequest,
  type AiRun,
  type Assessment,
  type AuditEntry,
  type CaseDetail,
  type CodingCandidate,
  type EditOp,
  type Me,
  type SourceText,
  type Task,
  type TenantSettings,
  type VersionDetail,
} from "./api";
import { useToken } from "./auth";

export function useMe() {
  const token = useToken();
  return useQuery({ queryKey: ["me"], queryFn: () => apiGet<Me>("/me", token), retry: false });
}

export function useQueue() {
  const token = useToken();
  return useQuery({
    queryKey: ["tasks"],
    queryFn: () => apiGet<Task[]>("/tasks", token),
    refetchInterval: 5000,
  });
}

export function useCase(caseId: string) {
  const token = useToken();
  return useQuery({
    queryKey: ["case", caseId],
    queryFn: () => apiGet<CaseDetail>(`/cases/${caseId}`, token),
    refetchInterval: 5000,
  });
}

export function useVersion(caseId: string, version: number | undefined) {
  const token = useToken();
  return useQuery({
    queryKey: ["case", caseId, "version", version],
    queryFn: () => apiGet<VersionDetail>(`/cases/${caseId}/versions/${version}`, token),
    enabled: version !== undefined,
    refetchInterval: 5000,
  });
}

export function useAssessment(caseId: string, version: number | undefined) {
  const token = useToken();
  return useQuery({
    queryKey: ["case", caseId, "version", version, "assessment"],
    queryFn: () => apiGet<Assessment>(`/cases/${caseId}/versions/${version}/assessment`, token),
    enabled: version !== undefined,
  });
}

export function useAiRuns(caseId: string, version: number | undefined) {
  const token = useToken();
  return useQuery({
    queryKey: ["case", caseId, "version", version, "ai"],
    queryFn: () => apiGet<AiRun[]>(`/cases/${caseId}/versions/${version}/ai`, token),
    enabled: version !== undefined,
    refetchInterval: 5000,
  });
}

export function useSource(caseId: string) {
  const token = useToken();
  return useQuery({
    queryKey: ["case", caseId, "source"],
    queryFn: async () => {
      try {
        return await apiGet<SourceText>(`/cases/${caseId}/source`, token);
      } catch (e) {
        if (e instanceof Error && "status" in e && (e as { status: number }).status === 404) {
          return null;
        }
        throw e;
      }
    },
  });
}

export function useCaseAudit(caseId: string) {
  const token = useToken();
  return useQuery({
    queryKey: ["case", caseId, "audit"],
    queryFn: () => apiGet<AuditEntry[]>(`/cases/${caseId}/audit`, token),
  });
}

export async function searchCodes(token: string, kind: string, term: string) {
  const params = new URLSearchParams({ term });
  const result = await apiGet<{ release: string; candidates: CodingCandidate[] }>(
    `/coding/${kind}/search?${params.toString()}`,
    token,
  );
  return result;
}

/** Edits a draft version; refreshes the case. The caller passes the expected revision. */
export function useEdit(caseId: string, version: number) {
  const token = useToken();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (args: { revision: number; changes: EditOp[]; reason?: string }) =>
      apiRequest<VersionDetail>("PATCH", `/cases/${caseId}/versions/${version}`, token, {
        expected_revision: args.revision,
        changes: args.changes,
        reason: args.reason ?? null,
      }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["case", caseId] }),
  });
}

export function useTenantSettings() {
  const token = useToken();
  return useQuery({
    queryKey: ["admin", "tenant"],
    queryFn: () => apiGet<TenantSettings>("/admin/tenant", token),
  });
}
