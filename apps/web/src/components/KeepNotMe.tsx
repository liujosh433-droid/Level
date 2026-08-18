"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import styles from "./KeepNotMe.module.css";

type Props = {
  entity: "person" | "usual" | "priority";
  id: string;
  initialStatus?: "proposed" | "kept" | "not_me";
  onChange?: (newStatus: "kept" | "not_me") => void;
};

export default function KeepNotMe({ entity, id, initialStatus = "proposed", onChange }: Props) {
  const [status, setStatus] = useState(initialStatus);
  const [busy, setBusy] = useState(false);

  async function set(newStatus: "kept" | "not_me") {
    if (busy) return;
    setBusy(true);
    try {
      await api.post("/v1/profile/keep_not_me", { entity, id, status: newStatus });
      setStatus(newStatus);
      onChange?.(newStatus);
    } finally {
      setBusy(false);
    }
  }

  if (status === "kept") {
    return (
      <div className={styles.settled}>
        <span className="pill pill--signal">Kept</span>
        <button
          type="button"
          onClick={() => set("not_me")}
          className={styles.undo}
          disabled={busy}
        >
          Undo
        </button>
      </div>
    );
  }
  if (status === "not_me") {
    return (
      <div className={styles.settled}>
        <span className="pill">Not me</span>
        <button type="button" onClick={() => set("kept")} className={styles.undo} disabled={busy}>
          Undo
        </button>
      </div>
    );
  }

  return (
    <div className={styles.row}>
      <button className="button-primary" onClick={() => set("kept")} disabled={busy}>
        Keep
      </button>
      <button className="button-ghost" onClick={() => set("not_me")} disabled={busy}>
        Not me
      </button>
    </div>
  );
}
