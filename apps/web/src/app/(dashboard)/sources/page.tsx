"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { SourcesStatus } from "@/lib/types";
import styles from "./sources.module.css";

export default function SourcesPage() {
  const [status, setStatus] = useState<SourcesStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [daysBack, setDaysBack] = useState(14);
  const [daysForward, setDaysForward] = useState(28);

  const load = useCallback(async () => {
    const s = await api.get<SourcesStatus>("/v1/sources/status");
    setStatus(s);
    if (s.days_back) setDaysBack(s.days_back);
    if (s.days_forward) setDaysForward(s.days_forward);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function sync() {
    setBusy(true);
    setMessage(null);
    try {
      const r = await api.post<{ refresh: { added: number; removed: number; updated: number } }>(
        "/v1/sources/sync",
        {},
      );
      setMessage(
        `Added ${r.refresh.added}, updated ${r.refresh.updated}, removed ${r.refresh.removed}.`,
      );
      await load();
    } catch {
      setMessage("Sync failed. Check the API log.");
    } finally {
      setBusy(false);
    }
  }

  async function saveWindow() {
    setBusy(true);
    try {
      await api.post("/v1/sources/window", { days_back: daysBack, days_forward: daysForward });
      setMessage("Calendar window saved.");
      await load();
    } finally {
      setBusy(false);
    }
  }

  async function disconnect() {
    if (!confirm("Disconnect Google and wipe local data for this account?")) return;
    setBusy(true);
    try {
      await api.del("/v1/me");
      setMessage("Disconnected. Reload to sign in again.");
      await load();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={styles.wrap}>
      <header>
        <h1>Sources</h1>
        <p>Level only talks to Google Calendar (read/write) and Gmail (send).</p>
      </header>

      <section className="card">
        {status?.google_connected ? (
          <div className={styles.connected}>
            <div>
              <strong>Google is connected</strong>
              <div className={styles.meta}>
                {status.email}
                {status.calendars && status.calendars.length > 0
                  ? ` · ${status.calendars.map((c) => c.summary || c.id).join(", ")}`
                  : ` · calendar ${status.calendar_id ?? "primary"}`}
                {" · last pull "}
                {status.last_pull_at ? new Date(status.last_pull_at).toLocaleString() : "never"}
              </div>
              {status.last_error ? (
                <div className={styles.meta}>Last sync error: {status.last_error}</div>
              ) : null}
            </div>
            <div className={styles.actions}>
              <button className="button-ghost" onClick={sync} disabled={busy}>
                {busy ? "Syncing..." : "Sync now"}
              </button>
              <button className="button-ghost" onClick={disconnect} disabled={busy}>
                Disconnect
              </button>
            </div>
          </div>
        ) : (
          <div className={styles.connected}>
            <div>
              <strong>Not connected</strong>
              <div className={styles.meta}>Grant read/write on Calendar and send on Gmail.</div>
            </div>
            <a className="button-primary" href="/v1/auth/google/start">
              Connect Google
            </a>
          </div>
        )}
      </section>

      <section className="card">
        <h2>Calendar window</h2>
        <p className={styles.meta}>
          Smaller windows cost fewer Gemini tokens. Wider windows detect more usuals.
        </p>
        <div className={styles.sliderRow}>
          <label>
            Days back: <strong>{daysBack}</strong>
            <input
              type="range"
              min={7}
              max={90}
              step={7}
              value={daysBack}
              onChange={(e) => setDaysBack(Number(e.target.value))}
            />
          </label>
          <label>
            Days forward: <strong>{daysForward}</strong>
            <input
              type="range"
              min={7}
              max={90}
              step={7}
              value={daysForward}
              onChange={(e) => setDaysForward(Number(e.target.value))}
            />
          </label>
          <button className="button-primary" onClick={saveWindow} disabled={busy}>
            Save
          </button>
        </div>
      </section>

      <section className="card">
        <h2>AI calls today</h2>
        <p className={styles.meta}>
          Total Gemini calls in this account&apos;s log:{" "}
          <strong>{status?.ai_calls_total ?? 0}</strong>. See the live trace under{" "}
          <a href="/admin/traces">Admin - traces</a>.
        </p>
      </section>

      {message && <p className={styles.status}>{message}</p>}
    </div>
  );
}
