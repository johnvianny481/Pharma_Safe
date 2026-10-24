/** Calls the PharmaSafe API through the dev-server proxy (`/api` → API). */
export async function apiGet<T>(path: string, accessToken: string): Promise<T> {
  const response = await fetch(`/api${path}`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) throw new Error(`API returned ${response.status}`);
  return (await response.json()) as T;
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
