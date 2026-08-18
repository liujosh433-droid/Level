"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { TraceEntry } from "@/lib/types";
import styles from "./traces.module.css";

export default function TracesPage() {
  const [traces, setTraces] = useState<TraceEntry[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await api.get<{ traces: TraceEntry[] }>("/v1/admin/traces?limit=50");
      setTraces(r.traces);
      setError(null);
    } catch (e) {
      setError("Admin traces are disabled in this environment.");
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [load]);

  return (
    <div className={styles.wrap}>
      <header>
        <h1>Live agent traces</h1>
        <p>
          The last 50 Gemini calls in this account&apos;s log. Auto-refreshes every 3 seconds -
          used in the demo video as Proof of Action.
        </p>
      </header>
      {error && <p className={styles.error}>{error}</p>}
      <table className={styles.table}>
        <thead>
          <tr>
            <th>When</th>
            <th>Agent</th>
            <th>Model</th>
            <th>Cost</th>
            <th>Latency</th>
            <th>Flags</th>
          </tr>
        </thead>
        <tbody>
          {traces.map((t) => (
            <tr key={t.audit_id}>
              <td>{new Date(t.created_at).toLocaleTimeString()}</td>
              <td>{t.agent}</td>
              <td>{t.model}</td>
              <td>${t.cost_estimate_usd.toFixed(6)}</td>
              <td>{t.latency_ms} ms</td>
              <td>
                {t.hallucinated && <span className="pill">hallucinated</span>}{" "}
                {t.blocked_by_safety && <span className="pill">safety</span>}
              </td>
            </tr>
          ))}
          {traces.length === 0 && !error && (
            <tr>
              <td colSpan={6} className={styles.empty}>
                No agent calls yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
