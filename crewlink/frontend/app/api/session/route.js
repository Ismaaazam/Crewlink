import { cookies } from "next/headers";
import { NextResponse } from "next/server";

const BACKEND = process.env.BACKEND_INTERNAL_URL || "http://backend:8000";
export const dynamic = "force-dynamic";

// POST: log in. The Django token goes into an httpOnly cookie, so page scripts never see it.
export async function POST(request) {
  const body = await request.text();
  let r;
  try {
    r = await fetch(`${BACKEND}/api/auth/login/`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body, cache: "no-store",
    });
  } catch {
    return NextResponse.json({ detail: "Backend unreachable." }, { status: 502 });
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) return NextResponse.json({ detail: data.detail || "Login failed." }, { status: r.status });
  if (data.role !== "leader") {
    return NextResponse.json({ detail: "This dashboard is for union leadership accounts." }, { status: 403 });
  }
  cookies().set("crewlink_token", data.token, {
    httpOnly: true, sameSite: "lax", path: "/", secure: process.env.NODE_ENV === "production",
  });
  return NextResponse.json({ role: data.role, local: data.local });
}

// DELETE: log out (drops the cookie).
export async function DELETE() {
  cookies().delete("crewlink_token");
  return NextResponse.json({ ok: true });
}
