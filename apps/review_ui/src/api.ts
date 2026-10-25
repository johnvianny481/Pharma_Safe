/** Calls the PharmaSafe API through the dev-server proxy (`/api` → API). */
export async function apiGet<T>(path: string, accessToken: string): Promise<T> {
  const response = await fetch(`/api${path}`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) throw new Error(`API returned ${response.status}`);
  return (await response.json()) as T;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

/** POST JSON. Pass an idempotency key for intake calls, and reuse it when retrying. */
export async function apiPost<T>(
  path: string,
  body: unknown,
  accessToken: string,
  idempotencyKey?: string,
): Promise<T> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${accessToken}`,
    "Content-Type": "application/json",
  };
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  const response = await fetch(`/api${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new ApiError(response.status, `API returned ${response.status}`);
  return (await response.json()) as T;
}

export interface IntakeAck {
  submission_id: string;
  duplicate: boolean;
  reports: { index: number; case_id: string; case_number: string }[];
}

export interface Me {
  subject: string;
  username: string;
  display_name: string;
  tenant_id: string | null;
  roles: string[];
  /** `null` means unrestricted. */
  products: string[] | null;
  regions: string[] | null;
}
