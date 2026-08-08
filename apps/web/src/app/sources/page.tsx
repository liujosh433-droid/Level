"use client";

import { FormEvent, Suspense, useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { API_BASE, createGuest } from "@/lib/api";
import styles from "./sources.module.css";

type Fact = { fact_id: string; type: string; statement: string };
type Summary = {
  accepted: number;
  blocked: number;
  skipped: number;
  facts: number;
  detail: string;
  profile_bullets?: number;
  contradictions?: number;
};
type Bullet = {
  bullet_id: string;
  category: string;
  text: string;
  status: string;
};
type Profile = {
  manifesto: string | null;
  needs_review: boolean;
  bullets: Bullet[];
  contradictions: { contradiction_id: string; summary: string; status: string }[];
  fact_count: number;
};

function SourcesInner() {
  const params = useSearchParams();
  const [userId, setUserId] = useState("");
  const [email, setEmail] = useState<string | null>(null);
  const [googleConnected, setGoogleConnected] = useState(false);
  const [facts, setFacts] = useState<Fact[]>([]);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  useEffect(() => {
    const fromUrl = params.get("user_id");
    const stored = typeof window !== "undefined" ? localStorage.getItem("level_user_id") : null;
    const id = fromUrl || stored || "";
    if (fromUrl) localStorage.setItem("level_user_id", fromUrl);
    if (params.get("connected") === "1") {
      setGoogleConnected(true);
      setStatus("Google connected. Hit Sync now to pull Calendar + Drive.");
    }
    setUserId(id);
  }, [params]);

  useEffect(() => {
    if (!userId) return;
    void refresh(userId);
  }, [userId]);

  async function refresh(uid: string) {
    try {
      const me = await fetch(`${API_BASE}/v1/auth/me?user_id=${encodeURIComponent(uid)}`);
      if (me.ok) {
        const data = await me.json();
        setEmail(data.email ?? null);
        setGoogleConnected(Boolean(data.google_connected));
        if (!data.google_connected) {
          setStatus("Google session missing on the API — click Connect Google again, then Sync.");
        }
      } else if (me.status === 404) {
        setGoogleConnected(false);
        setStatus("User not found on API (restart wiped session). Click Connect Google again.");
      }
      const factsRes = await fetch(
        `${API_BASE}/v1/sources/facts?user_id=${encodeURIComponent(uid)}&limit=40`,
      );
      if (factsRes.ok) setFacts(await factsRes.json());
      const profileRes = await fetch(
        `${API_BASE}/v1/sources/profile?user_id=${encodeURIComponent(uid)}`,
      );
      if (profileRes.ok) setProfile(await profileRes.json());
    } catch {
      /* ignore until API up */
    }
  }

  async function startGuest() {
    setBusy(true);
    setStatus(null);
    try {
      const me = await createGuest("Real parent");
      localStorage.setItem("level_user_id", me.user_id);
      setUserId(me.user_id);
      setStatus(`Created user ${me.user_id.slice(0, 8)}… — upload ChatGPT export next.`);
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function onChatGPT(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!userId) {
      setStatus("Create a guest user or Connect Google first, then upload.");
      return;
    }
    const input = (e.currentTarget.elements.namedItem("export") as HTMLInputElement) || null;
    const file = input?.files?.[0];
    if (!file) return;
    setBusy(true);
    setStatus("Ingesting ChatGPT export — this can take a few minutes…");
    try {
      const body = new FormData();
      body.set("user_id", userId);
      body.set("file", file);
      body.set("max_messages", "120");
      const res = await fetch(`${API_BASE}/v1/sources/chatgpt`, { method: "POST", body });
      const data: Summary = await res.json();
      if (!res.ok) throw new Error(JSON.stringify(data));
      setStatus(
        `ChatGPT: accepted ${data.accepted}, facts ${data.facts}. ${data.detail}`,
      );
      await refresh(userId);
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function syncGoogle() {
    if (!userId) return;
    setBusy(true);
    setStatus("Syncing Calendar + Drive…");
    try {
      const body = new FormData();
      body.set("user_id", userId);
      const res = await fetch(`${API_BASE}/v1/sources/google/sync`, { method: "POST", body });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
      setStatus(
        `Google sync: accepted ${data.accepted}, facts ${data.facts}. ${data.detail}`,
      );
      await refresh(userId);
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function resetMemory() {
    if (!userId) return;
    if (!window.confirm("Clear this user's Memory Bank facts and re-sync after?")) return;
    setBusy(true);
    try {
      const body = new FormData();
      body.set("user_id", userId);
      const res = await fetch(`${API_BASE}/v1/sources/reset`, { method: "POST", body });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
      setProfile(null);
      setStatus(`Cleared ${data.cleared} memory items. Sync Google again (and re-upload ChatGPT if needed).`);
      await refresh(userId);
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function submitNote(e: FormEvent) {
    e.preventDefault();
    if (!userId || note.trim().length < 20) return;
    setBusy(true);
    try {
      const res = await fetch(`${API_BASE}/v1/sources/note`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: userId, text: note.trim() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
      setNote("");
      setStatus(`Note ingested (+${data.facts} facts).`);
      await refresh(userId);
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function setBulletStatus(bulletId: string, next: "accepted" | "rejected") {
    if (!userId) return;
    setBusy(true);
    try {
      const res = await fetch(`${API_BASE}/v1/sources/profile/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id: userId,
          mark_reviewed: false,
          bullets: [{ bullet_id: bulletId, status: next }],
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
      setProfile(data);
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function confirmProfile() {
    if (!userId || !profile) return;
    setBusy(true);
    try {
      const pending = profile.bullets.filter((b) => b.status === "pending");
      const res = await fetch(`${API_BASE}/v1/sources/profile/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id: userId,
          mark_reviewed: true,
          bullets: pending.map((b) => ({ bullet_id: b.bullet_id, status: "accepted" })),
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
      setProfile(data);
      setStatus("Profile confirmed. Ask Level with a real decision.");
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className={styles.page}>
      <header className={styles.header}>
        <Link href="/" className={styles.brand}>
          Level
        </Link>
        <nav className={styles.nav}>
          <Link href="/sources">Sources</Link>
          <Link href={userId ? `/session?user_id=${userId}` : "/session"}>Session</Link>
        </nav>
      </header>

      <h1 className={styles.title}>Your sources</h1>
      <p className={styles.sub}>
        Connect the messy real inputs — ChatGPT history, Calendar, Drive — so Level
        can challenge you with your own evidence.
      </p>

      <section className={styles.card}>
        <h2>Identity</h2>
        <p className={styles.meta}>
          {userId
            ? `Active user ${userId.slice(0, 8)}…${email ? ` · ${email}` : ""}`
            : "No user yet — start as guest (ChatGPT only) or connect Google."}
        </p>
        <div className={styles.row}>
          <button type="button" className={styles.ghost} disabled={busy} onClick={startGuest}>
            Start as guest
          </button>
          <a className={styles.primary} href={`${API_BASE}/v1/auth/google/start`}>
            {googleConnected ? "Reconnect Google" : "Connect Google"}
          </a>
          <button type="button" className={styles.ghost} disabled={!googleConnected || busy} onClick={syncGoogle}>
            Sync Calendar + Drive
          </button>
          <button type="button" className={styles.ghost} disabled={!userId || busy} onClick={resetMemory}>
            Clear memory
          </button>
        </div>
      </section>

      <section className={styles.card}>
        <h2>ChatGPT export</h2>
        <p className={styles.meta}>
          ChatGPT → Settings → Data controls → Export data → upload the zip (or
          conversations.json). We only keep <em>your</em> messages, not the assistant&apos;s.
        </p>
        <form onSubmit={onChatGPT} className={styles.row}>
          <input name="export" type="file" accept=".zip,.json,application/zip,application/json" required />
          <button type="submit" className={styles.primary} disabled={busy || !userId}>
            Upload &amp; ingest
          </button>
        </form>
        {!userId && (
          <p className={styles.warn}>Start as guest or Connect Google first.</p>
        )}
      </section>

      <section className={styles.card}>
        <h2>Quick note</h2>
        <form onSubmit={submitNote}>
          <textarea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            rows={3}
            placeholder="Something true about your life Level should know…"
            disabled={busy || !userId}
          />
          <div className={styles.row}>
            <button type="submit" className={styles.ghost} disabled={busy || !userId || note.trim().length < 20}>
              Save note
            </button>
          </div>
        </form>
      </section>

      {status && <p className={styles.status}>{status}</p>}

      {profile && profile.bullets.length > 0 && (
        <section className={styles.card}>
          <h2>Here&apos;s who I think you are</h2>
          <p className={styles.meta}>
            Reject anything wrong. Confirm when it looks right — Level will use this
            when challenging your decisions.
            {profile.needs_review ? " · Needs your review" : " · Reviewed"}
          </p>
          {profile.manifesto && <p className={styles.manifesto}>{profile.manifesto}</p>}
          <ul className={styles.profileList}>
            {profile.bullets.map((b) => (
              <li key={b.bullet_id} className={styles.profileItem}>
                <span className={styles.cat}>{b.category}</span>
                <p>{b.text}</p>
                <div className={styles.row}>
                  <button
                    type="button"
                    className={styles.ghost}
                    disabled={busy || b.status === "accepted"}
                    onClick={() => setBulletStatus(b.bullet_id, "accepted")}
                  >
                    Keep
                  </button>
                  <button
                    type="button"
                    className={styles.ghost}
                    disabled={busy}
                    onClick={() => setBulletStatus(b.bullet_id, "rejected")}
                  >
                    Reject
                  </button>
                </div>
              </li>
            ))}
          </ul>
          {profile.contradictions.length > 0 && (
            <div className={styles.tensions}>
              <h3>Tensions</h3>
              <ul>
                {profile.contradictions.map((c) => (
                  <li key={c.contradiction_id}>{c.summary}</li>
                ))}
              </ul>
            </div>
          )}
          <div className={styles.row}>
            <button type="button" className={styles.primary} disabled={busy} onClick={confirmProfile}>
              Looks right — continue
            </button>
            {userId && (
              <Link href={`/session?user_id=${userId}`} className={styles.ghost}>
                Ask Level →
              </Link>
            )}
          </div>
        </section>
      )}

      <section className={styles.facts}>
        <div className={styles.factsHead}>
          <h2>Memory Bank ({facts.length} facts)</h2>
          {userId && facts.length > 0 && (
            <Link href={`/session?user_id=${userId}`} className={styles.primary}>
              Ask Level →
            </Link>
          )}
        </div>
        {facts.length === 0 ? (
          <p className={styles.meta}>Nothing ingested yet for this user.</p>
        ) : (
          <ul>
            {facts.map((f) => (
              <li key={f.fact_id}>
                <span>{f.type}</span>
                {f.statement}
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}

export default function SourcesPage() {
  return (
    <Suspense fallback={<main className={styles.page} />}>
      <SourcesInner />
    </Suspense>
  );
}
