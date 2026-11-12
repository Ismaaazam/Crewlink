export const dynamic = "force-dynamic";

async function getHealth() {
  const base = process.env.BACKEND_INTERNAL_URL || "http://localhost:8000";
  try {
    const res = await fetch(`${base}/api/health/`, { cache: "no-store" });
    return { ok: res.ok, body: await res.json() };
  } catch (e) {
    return { ok: false, body: { status: "unreachable" } };
  }
}

export default async function Home() {
  const health = await getHealth();
  return (
    <main>
      <h1>CrewLink</h1>
      <p>Backend: {health.ok ? "connected" : "not reachable"}</p>
      <pre>{JSON.stringify(health.body, null, 2)}</pre>
    </main>
  );
}
