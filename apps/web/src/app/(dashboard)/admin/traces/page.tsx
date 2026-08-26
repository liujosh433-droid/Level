"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import type { TraceEntry, TraceGroup, TracesResponse } from "@/lib/types";
import styles from "./traces.module.css";

type ViewMode = "waterfall" | "table";

export default function TracesPage() {
  const [traces, setTraces] = useState<TraceEntry[]>([]);
  const [grouped, setGrouped] = useState<TraceGroup[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<ViewMode>("waterfall");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const load = useCallback(async () => {
    try {
      const r = await api.get<TracesResponse>("/v1/admin/traces?limit=100");
      setTraces(r.traces);
      setGrouped(r.grouped ?? []);
      setError(null);
    } catch {
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
      <header className={styles.headerRow}>
        <div>
          <h1>Live agent traces</h1>
          <p>
            Every Gemini/ADK call in this account, grouped by trace. Auto-refreshes every 3 seconds -
            used in the demo video as Proof of Action.
          </p>
        </div>
        <div className={styles.viewToggle} role="tablist" aria-label="View mode">
          <button
            type="button"
            role="tab"
            aria-selected={view === "waterfall"}
            className={view === "waterfall" ? styles.toggleActive : undefined}
            onClick={() => setView("waterfall")}
          >
            Waterfall
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={view === "table"}
            className={view === "table" ? styles.toggleActive : undefined}
            onClick={() => setView("table")}
          >
            Table
          </button>
        </div>
      </header>

      {error && <p className={styles.error}>{error}</p>}

      {view === "waterfall" ? (
        <WaterfallView
          groups={grouped}
          expanded={expanded}
          onToggle={(traceId) =>
            setExpanded((prev) => ({ ...prev, [traceId]: !prev[traceId] }))
          }
          empty={grouped.length === 0 && !error}
        />
      ) : (
        <TableView traces={traces} empty={traces.length === 0 && !error} />
      )}
    </div>
  );
}

function WaterfallView({
  groups,
  expanded,
  onToggle,
  empty,
}: {
  groups: TraceGroup[];
  expanded: Record<string, boolean>;
  onToggle: (traceId: string) => void;
  empty: boolean;
}) {
  const maxLatency = useMemo(() => {
    return groups.reduce((max, g) => Math.max(max, g.total_latency_ms), 0) || 1;
  }, [groups]);

  if (empty) {
    return <p className={styles.empty}>No agent calls yet.</p>;
  }
  return (
    <div className={styles.groupList}>
      {groups.map((g) => {
        const isOpen = expanded[g.trace_id] ?? false;
        return (
          <div key={g.trace_id} className={styles.group}>
            <div
              className={styles.groupHeader}
              role="button"
              tabIndex={0}
              onClick={() => onToggle(g.trace_id)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onToggle(g.trace_id);
                }
              }}
              aria-expanded={isOpen}
            >
              <span className={styles.groupMeta}>
                {new Date(g.started_at).toLocaleTimeString()}
              </span>
              <span className={styles.groupAgent}>
                {g.root.agent} → {g.row_count - 1 > 0 ? `${g.row_count - 1} spans` : "single call"}
              </span>
              <span className={styles.groupCost}>${g.total_cost_usd.toFixed(6)}</span>
              <span className={styles.groupLatency}>{g.total_latency_ms} ms</span>
              <span className={styles.pillRow}>
                {g.any_hallucinated && <span className={`${styles.pill} ${styles.pillWarn}`}>hallucinated</span>}
                {g.any_fallback && <span className={`${styles.pill} ${styles.pillInfo}`}>fallback</span>}
                {g.root.blocked_by_safety && <span className={`${styles.pill} ${styles.pillWarn}`}>safety</span>}
              </span>
            </div>
            {isOpen && (
              <div className={styles.spans}>
                <SpanRow trace={g.root} maxMs={maxLatency} />
                {g.children.map((c) => (
                  <div key={c.audit_id} className={styles.spanChildIndent}>
                    <SpanRow trace={c} maxMs={maxLatency} />
                  </div>
                ))}
                <pre className={styles.raw}>
                  {JSON.stringify({ root: g.root, children: g.children }, null, 2)}
                </pre>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function SpanRow({ trace, maxMs }: { trace: TraceEntry; maxMs: number }) {
  const pct = Math.max(2, Math.round(((trace.latency_ms || 0) / maxMs) * 100));
  return (
    <div className={styles.span}>
      <span className={styles.spanLabel}>
        {trace.agent}
        {trace.fallback_used ? ` → ${trace.fallback_used}` : ""}
        {trace.turns_taken && trace.turns_taken > 1 ? ` (${trace.turns_taken} turns)` : ""}
      </span>
      <span>{trace.latency_ms} ms</span>
      <div className={styles.spanBarWrap}>
        <div className={styles.spanBar} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

function TableView({ traces, empty }: { traces: TraceEntry[]; empty: boolean }) {
  return (
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
              <span className={styles.pillRow}>
                {t.hallucinated && <span className={`${styles.pill} ${styles.pillWarn}`}>hallucinated</span>}
                {t.blocked_by_safety && <span className={`${styles.pill} ${styles.pillWarn}`}>safety</span>}
                {t.fallback_used && <span className={`${styles.pill} ${styles.pillInfo}`}>{t.fallback_used}</span>}
                {t.turns_taken && t.turns_taken > 1 && (
                  <span className={`${styles.pill} ${styles.pillGood}`}>{t.turns_taken} turns</span>
                )}
              </span>
            </td>
          </tr>
        ))}
        {empty && (
          <tr>
            <td colSpan={6} className={styles.empty}>
              No agent calls yet.
            </td>
          </tr>
        )}
      </tbody>
    </table>
  );
}
