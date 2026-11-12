"use client";
import { useCallback, useEffect, useRef, useState } from "react";

const LIMIT = 120;
const len = (s) => [...s].length; // counts characters the same way the backend does

async function api(path, method = "GET", body) {
  const r = await fetch(`/api/proxy?path=${encodeURIComponent(path)}`, {
    method, headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json().catch(() => null);
  return { ok: r.ok, status: r.status, data };
}

function errText(data) {
  if (!data) return "Request failed.";
  if (data.detail) return data.detail;
  return Object.entries(data).map(([k, v]) => `${k}: ${[].concat(v).join(" ")}`).join("; ");
}

const EMPTY = { title: "", body: "", push_preview: "", needs_ack: false, audience: [] };
const toForm = (a) => ({
  title: a.title, body: a.body, push_preview: a.push_preview,
  needs_ack: a.needs_ack, audience: a.audience_classifications,
});
const box = { border: "1px solid #bbb", padding: 12, marginBottom: 16 };

export default function Page() {
  const [me, setMe] = useState(undefined); // undefined = loading, null = logged out
  useEffect(() => {
    api("/me/").then((r) => setMe(r.ok && r.data.role === "leader" ? r.data : null));
  }, []);
  if (me === undefined) return <main style={{ padding: 24 }}>Loading…</main>;
  if (!me) return <Login onDone={setMe} />;
  return <Dashboard me={me} onLogout={() => setMe(null)} />;
}

function Login({ onDone }) {
  const [u, setU] = useState("");
  const [p, setP] = useState("");
  const [err, setErr] = useState("");
  async function submit(e) {
    e.preventDefault();
    setErr("");
    const r = await fetch("/api/session", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: u, password: p }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) return setErr(d.detail || "Login failed.");
    const m = await api("/me/");
    onDone(m.ok ? m.data : null);
  }
  return (
    <main style={{ padding: 24, maxWidth: 360 }}>
      <h1>CrewLink – Leadership</h1>
      <form onSubmit={submit}>
        <p><label>Username<br /><input value={u} onChange={(e) => setU(e.target.value)} autoFocus /></label></p>
        <p><label>Password<br /><input type="password" value={p} onChange={(e) => setP(e.target.value)} /></label></p>
        <button type="submit">Log in</button>
        {err && <p role="alert" style={{ color: "#b00" }}>{err}</p>}
      </form>
    </main>
  );
}

function Dashboard({ me, onLogout }) {
  const [list, setList] = useState([]);
  const [audiences, setAudiences] = useState([]);
  const [locals, setLocals] = useState([me.local]); // whatever the server returns; one today
  const [current, setCurrent] = useState(null); // announcement as stored on the server
  const [form, setForm] = useState(EMPTY);
  const [prompt, setPrompt] = useState("");
  const [msg, setMsg] = useState(null); // { kind: "ok" | "err", text }
  const [busy, setBusy] = useState("");
  const [progress, setProgress] = useState(null);
  const [sendNote, setSendNote] = useState("");

  const loadList = useCallback(async () => {
    const r = await api("/announcements/");
    if (r.status === 401) return onLogout();
    if (r.ok) setList(r.data.results);
  }, [onLogout]);

  useEffect(() => {
    loadList();
    api("/announcements/audiences/").then((r) => {
      if (r.ok) { setAudiences(r.data.classifications); setLocals([r.data.local]); }
    });
  }, [loadList]);

  const status = current?.status || "none";
  const dirty = current
    ? JSON.stringify(toForm(current)) !== JSON.stringify(form)
    : form.title || form.body || form.push_preview;
  const overLimit = len(form.push_preview) > LIMIT;
  const locked = status === "sent";

  function select(a) {
    setCurrent(a); setForm(toForm(a)); setMsg(null); setSendNote(""); setProgress(null);
  }
  function newOne() { setCurrent(null); setForm(EMPTY); setMsg(null); setSendNote(""); setProgress(null); }
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const toggleAud = (c) =>
    setForm({ ...form, audience: form.audience.includes(c) ? form.audience.filter((x) => x !== c) : [...form.audience, c] });

  async function run(name, fn) {
    setBusy(name); setMsg(null);
    try { await fn(); } finally { setBusy(""); }
  }

  const generate = () => run("ai", async () => {
    const r = await api("/announcements/draft/", "POST", {
      prompt, needs_ack: form.needs_ack, audience_classifications: form.audience,
    });
    if (r.ok) {
      select(r.data.announcement);
      setMsg({ kind: "ok", text: `AI draft created (${r.data.provider}). It is a DRAFT: review, edit, then approve.` });
      loadList();
    } else if (r.data?.fallback === "manual") {
      setMsg({ kind: "err", text: "AI drafting is unavailable. Enter the announcement manually below." });
    } else setMsg({ kind: "err", text: errText(r.data) });
  });

  const save = () => run("save", async () => {
    const payload = {
      title: form.title, body: form.body, push_preview: form.push_preview,
      needs_ack: form.needs_ack, audience_classifications: form.audience,
    };
    const r = current ? await api(`/announcements/${current.id}/`, "PATCH", payload) : await api("/announcements/", "POST", payload);
    if (r.ok) {
      const wasApproved = current && current.status === "approved";
      select(r.data);
      setMsg({ kind: "ok", text: wasApproved ? "Saved. Approval was cleared: approve again before sending." : "Saved as draft." });
      loadList();
    } else setMsg({ kind: "err", text: errText(r.data) });
  });

  const approve = () => run("approve", async () => {
    const r = await api(`/announcements/${current.id}/approve/`, "POST");
    if (r.ok) { setCurrent(r.data); setMsg({ kind: "ok", text: "Approved. Nothing has been sent yet." }); loadList(); }
    else setMsg({ kind: "err", text: errText(r.data) });
  });

  const send = () => {
    const n = form.audience.length ? form.audience.join(", ") : "the whole local";
    if (!window.confirm(`Send to ${n}? This cannot be undone or edited afterwards.`)) return;
    run("send", async () => {
      const r = await api(`/announcements/${current.id}/send/`, "POST");
      if (r.ok) {
        setSendNote(`Send request accepted: ${r.data.counts.recipients} recipient(s) queued${r.data.already_sent ? " (already sent earlier)" : ""}. This is NOT confirmation that phones received it; watch the delivery progress below.`);
        setCurrent({ ...current, status: r.data.status, sent_at: r.data.sent_at });
        setProgress({ counts: r.data.counts });
        loadList();
      } else setMsg({ kind: "err", text: errText(r.data) });
    });
  };

  // poll delivery progress for the selected sent announcement
  const poll = useRef(null);
  useEffect(() => {
    clearInterval(poll.current);
    if (!current || current.status !== "sent") return;
    const tick = async () => {
      const r = await api(`/announcements/${current.id}/progress/`);
      if (r.ok) setProgress(r.data);
    };
    tick();
    poll.current = setInterval(tick, 5000);
    return () => clearInterval(poll.current);
  }, [current?.id, current?.status]);

  async function logout() { await fetch("/api/session", { method: "DELETE" }); onLogout(); }
  const c = progress?.counts;

  return (
    <main style={{ padding: 24, maxWidth: 960, fontFamily: "system-ui, sans-serif" }}>
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <h1>CrewLink – {me.local.name}</h1>
        <span>{me.username} · <button onClick={logout}>Log out</button></span>
      </header>
      <p>
        Local:{" "}
        {locals.length > 1
          ? <select defaultValue={me.local.id}>{locals.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}</select>
          : <strong>{locals[0].name}</strong>}{" "}
        <small>(set by the server from your account; you can only manage this local)</small>
      </p>

      <section style={box}>
        <h2>Announcements <button onClick={newOne}>+ New</button></h2>
        {list.length === 0 && <p>None yet.</p>}
        <ul>
          {list.map((a) => (
            <li key={a.id}>
              <button onClick={() => select(a)} style={{ fontWeight: current?.id === a.id ? "bold" : "normal" }}>
                {a.title || "(untitled)"}
              </button>{" "}
              <em>[{a.status}]</em>
            </li>
          ))}
        </ul>
      </section>

      {msg && <p role="status" style={{ color: msg.kind === "err" ? "#b00" : "#060" }}>{msg.text}</p>}

      <section style={box}>
        <h2>1. Create announcement <small>status: {status}</small></h2>
        {!locked && !current && (
          <p>
            <label>AI draft – describe it in a few notes<br />
              <textarea rows={2} cols={60} value={prompt} onChange={(e) => setPrompt(e.target.value)} /></label><br />
            <button onClick={generate} disabled={!prompt.trim() || busy}>{busy === "ai" ? "Generating…" : "Generate AI draft"}</button>{" "}
            <small>or type everything manually below</small>
          </p>
        )}
        <p><label>Title<br /><input size={60} value={form.title} onChange={set("title")} disabled={locked} /></label></p>
        <p><label>Body<br /><textarea rows={5} cols={60} value={form.body} onChange={set("body")} disabled={locked} /></label></p>
        <p>
          <label>Notification text (push preview)<br />
            <input size={60} value={form.push_preview} onChange={set("push_preview")} disabled={locked} /></label>{" "}
          <span style={{ color: overLimit ? "#b00" : "inherit" }}>{len(form.push_preview)}/{LIMIT}</span>
        </p>
        <p><label><input type="checkbox" checked={form.needs_ack} onChange={set("needs_ack")} disabled={locked} /> Members must acknowledge</label></p>
        <fieldset disabled={locked}>
          <legend>Audience (none ticked = whole local)</legend>
          {audiences.map((a) => (
            <label key={a.classification} style={{ marginRight: 12 }}>
              <input type="checkbox" checked={form.audience.includes(a.classification)} onChange={() => toggleAud(a.classification)} /> {a.classification} ({a.members})
            </label>
          ))}
        </fieldset>
        {!locked && (
          <p>
            <button onClick={save} disabled={overLimit || !form.title.trim() || !form.body.trim() || !dirty || busy}>
              {busy === "save" ? "Saving…" : current ? "Save changes" : "Save as draft"}
            </button>{" "}
            {current && status === "approved" && <small>Saving a change withdraws the approval.</small>}
            {dirty && current && <small> Unsaved changes.</small>}
          </p>
        )}
      </section>

      <section style={box}>
        <h2>Preview</h2>
        <div style={{ border: "1px solid #999", padding: 8, maxWidth: 360 }}>
          <strong>CrewLink</strong><br />{form.push_preview || <em>(notification text)</em>}
        </div>
        <h3>{form.title || <em>(title)</em>}</h3>
        <p style={{ whiteSpace: "pre-wrap" }}>{form.body || <em>(body)</em>}</p>
        <small>To: {form.audience.length ? form.audience.join(", ") : "whole local"} · {form.needs_ack ? "acknowledgement required" : "no acknowledgement required"}</small>
      </section>

      <section style={box}>
        <h2>2. Approval</h2>
        <p>Status: <strong>{status}</strong>{current?.approved_at && ` · approved ${new Date(current.approved_at).toLocaleString()}`}</p>
        <button onClick={approve} disabled={!current || status !== "draft" || dirty || busy}>
          {busy === "approve" ? "Approving…" : "Approve this exact text"}
        </button>{" "}
        <small>{!current ? "Save a draft first." : dirty ? "Save your changes first." : status === "draft" ? "Approving does not send anything." : ""}</small>
      </section>

      <section style={box}>
        <h2>3. Send</h2>
        <button onClick={send} disabled={status !== "approved" || dirty || busy}>
          {busy === "send" ? "Sending…" : "Send to members"}
        </button>{" "}
        <small>{status === "approved" ? "Creates every member's inbox item and queues the push." : "Only an approved announcement can be sent."}</small>
        {sendNote && <p role="status">{sendNote}</p>}
      </section>

      <section style={box}>
        <h2>4. Delivery progress <small>(refreshes every 5 s)</small></h2>
        {status !== "sent" ? <p>Not sent yet.</p> : !c ? <p>Loading…</p> : (
          <table cellPadding={4}>
            <tbody>
              <tr><td>Recipients (inbox items created)</td><td><strong>{c.recipients}</strong></td></tr>
              <tr><td>Sent (push accepted by provider)</td><td><strong>{c.pushed}</strong> of {c.recipients}</td></tr>
              <tr><td>Read</td><td><strong>{c.read}</strong> of {c.recipients}</td></tr>
              <tr><td>Acknowledged</td><td><strong>{c.acknowledged}</strong> of {c.recipients}{!current.needs_ack && " (not requested)"}</td></tr>
            </tbody>
          </table>
        )}
        {status === "sent" && <p><small>“Sent” means the push provider accepted it, not that a phone showed it. Offline members still get the item in their inbox when they reconnect.</small></p>}
      </section>
    </main>
  );
}
