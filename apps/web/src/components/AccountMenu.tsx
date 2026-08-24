"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { browserTimeZone } from "@/lib/dates";
import type { WhoAmI } from "@/lib/types";
import styles from "./AccountMenu.module.css";

const GENERIC = /^(you|me|self|myself|a parent)$/i;

function initials(name: string | null | undefined, email: string | null | undefined): string {
  const cleaned = (name || "").trim();
  const source =
    cleaned && !GENERIC.test(cleaned) ? cleaned : (email || "").split("@")[0] || "";
  const parts = source.split(/[\s._+\-]+/).filter(Boolean);
  if (parts.length >= 2) {
    return `${parts[0][0]}${parts[1][0]}`.toUpperCase();
  }
  if (parts[0] && parts[0].length >= 2) return parts[0].slice(0, 2).toUpperCase();
  if (parts[0]) return parts[0][0].toUpperCase();
  return "?";
}

function prettyName(who: WhoAmI | null): string {
  const name = who?.display_name?.trim() ?? "";
  if (name && !GENERIC.test(name)) return name;
  const local = (who?.email || "").split("@")[0] || "";
  if (!local) return "You";
  return local
    .split(/[._+\-]+/)
    .filter(Boolean)
    .map((p) => p.slice(0, 1).toUpperCase() + p.slice(1))
    .join(" ");
}

export default function AccountMenu() {
  const router = useRouter();
  const [who, setWho] = useState<WhoAmI | null>(null);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    void api
      .get<WhoAmI>("/v1/me")
      .then((me) => {
        setWho(me);
        const shown = prettyName(me);
        setName(shown === "You" ? "" : shown);
        const tz = browserTimeZone();
        if (me.tz !== tz) {
          void api.patch<WhoAmI>("/v1/me", { tz }).then((next) => setWho(next)).catch(() => undefined);
        }
      })
      .catch(() => setWho(null));
  }, []);

  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  async function saveName(e: FormEvent) {
    e.preventDefault();
    const next = name.trim();
    if (!next || saving) return;
    setSaving(true);
    setStatus(null);
    try {
      const me = await api.patch<WhoAmI>("/v1/me", { display_name: next });
      setWho(me);
      setName(prettyName(me));
      setStatus("Saved.");
      window.dispatchEvent(new CustomEvent("level:whoami", { detail: me }));
    } catch {
      setStatus("Couldn't save that name.");
    } finally {
      setSaving(false);
    }
  }

  async function logout() {
    try {
      await api.post("/v1/auth/logout", {});
    } catch {
      /* still bounce home */
    }
    window.location.href = "/";
  }

  const label = prettyName(who);
  const letters = initials(who?.display_name, who?.email);

  return (
    <div className={styles.wrap} ref={wrapRef}>
      <button
        type="button"
        className={styles.avatar}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Account menu for ${label}`}
        onClick={() => {
          setOpen((v) => !v);
          setStatus(null);
        }}
      >
        {letters}
      </button>
      {open ? (
        <div className={styles.menu} role="menu">
          <p className={styles.email}>{who?.email ?? "Signed in"}</p>
          <form onSubmit={saveName} className={styles.form}>
            <label className={styles.field}>
              <span>Your name</span>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="How should Level address you?"
                autoComplete="name"
              />
            </label>
            <button type="submit" className={styles.save} disabled={saving || !name.trim()}>
              {saving ? "Saving…" : "Save name"}
            </button>
          </form>
          {status ? <p className={styles.status}>{status}</p> : null}
          <button
            type="button"
            className={styles.item}
            onClick={() => {
              setOpen(false);
              router.push("/profile");
            }}
          >
            About me
          </button>
          <button type="button" className={styles.logout} onClick={() => void logout()}>
            Log out
          </button>
        </div>
      ) : null}
    </div>
  );
}
