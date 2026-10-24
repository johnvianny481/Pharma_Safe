import { useQuery } from "@tanstack/react-query";

interface Health {
  status: string;
  service: string;
}

async function fetchHealth(): Promise<Health> {
  const response = await fetch("/api/healthz");
  if (!response.ok) throw new Error(`API returned ${response.status}`);
  return (await response.json()) as Health;
}

/** Phase 0 shell. Queues, case workspace and signing arrive in Phase 6. */
export function App() {
  const health = useQuery({ queryKey: ["healthz"], queryFn: fetchHealth, retry: false });

  return (
    <main>
      <h1>PharmaSafe Review</h1>
      <p role="status">
        API:{" "}
        {health.isPending ? "checking…" : health.isSuccess ? health.data.status : "unreachable"}
      </p>
    </main>
  );
}
