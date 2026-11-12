import { cookies } from "next/headers";
import { NextResponse } from "next/server";

const BACKEND = process.env.BACKEND_INTERNAL_URL || "http://backend:8000";
export const dynamic = "force-dynamic";

// Same-origin proxy: the browser calls /api/proxy?path=/announcements/... and this server adds
// the token from the httpOnly cookie. Only leadership and own-identity paths are forwarded.
const ALLOWED = /^\/(announcements|me)\/[A-Za-z0-9\-\/]*$/;

async function forward(request, method) {
  const path = new URL(request.url).searchParams.get("path") || "";
  if (!ALLOWED.test(path) || !path.endsWith("/")) {
    return NextResponse.json({ detail: "Not allowed." }, { status: 400 });
  }
  const token = cookies().get("crewlink_token")?.value;
  if (!token) return NextResponse.json({ detail: "Not logged in." }, { status: 401 });
  const init = {
    method, cache: "no-store",
    headers: { Authorization: `Token ${token}`, "Content-Type": "application/json" },
  };
  if (method !== "GET") init.body = (await request.text()) || "{}";
  let r;
  try {
    r = await fetch(`${BACKEND}/api${path}`, init);
  } catch {
    return NextResponse.json({ detail: "Backend unreachable." }, { status: 502 });
  }
  const text = await r.text();
  return new NextResponse(text, { status: r.status, headers: { "Content-Type": "application/json" } });
}

export const GET = (req) => forward(req, "GET");
export const POST = (req) => forward(req, "POST");
export const PATCH = (req) => forward(req, "PATCH");
