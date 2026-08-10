"use client";

import { FormEvent, Suspense, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import {
  AuthError,
  fetchMe,
  fetchProfile,
  profileChat,
  reviewProfile,
  type Profile,
} from "@/lib/api";
import styles from "./profile.module.css";

type ChatLine = { role: "you" | "level"; text: string };

function ProfileInner() {
  const router = useRouter();
  const [userId, setUserId] = useState("");
  const [profile, setProfile] = useState<Profile | null>(null);
  const [draft, setDraft] = useState("");
  const [chat, setChat] = useState<ChatLine[]>([]);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const me = await fetchMe();
        setUserId(me.user_id);
        setProfile(await fetchProfile());
      } catch (err) {
        if (err instanceof AuthError) {
          router.replace("/welcome");
          return;
        }
        setStatus(err instanceof Error ? err.message : String(err));
      }
    })();
  }, [router]);

  async function setBullet(bulletId: string, next: "accepted" | "rejected") {
    if (!userId) return;
    setBusy(true);
    try {
      const updated = await reviewProfile(
        [{ bullet_id: bulletId, status: next }],
        false,
      );
      setProfile(updated);
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function confirmAll() {
    if (!userId || !profile?.bullets) return;
    setBusy(true);
    try {
      const pending = profile.bullets.filter((b) => b.status === "pending");
      const updated = await reviewProfile(
        pending.map((b) => ({ bullet_id: b.bullet_id, status: "accepted" })),
        true,
      );
      setProfile(updated);
      setStatus("Profile confirmed.");
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function onChat(e: FormEvent) {
    e.preventDefault();
    if (!userId || draft.trim().length < 8 || busy) return;
    const message = draft.trim();
    setDraft("");
    setChat((prev) => [...prev, { role: "you", text: message }]);
    setBusy(true);
    try {
      const res = await profileChat(message);
      setProfile(res.profile);
      setChat((prev) => [...prev, { role: "level", text: res.reply }]);
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const bullets = profile?.bullets ?? [];

  return (
    <AppShell userId={userId}>
      <p className={styles.kicker}>Profile</p>
      <h1 className={styles.title}>Who Level thinks you are</h1>
      <p className={styles.sub}>
        Confirm what’s true. Tell Level anything to fix or add — it will remember.
      </p>

      {profile?.manifesto && <p className={styles.manifesto}>{profile.manifesto}</p>}

      {bullets.length === 0 ? (
        <p className={styles.meta}>
          No profile yet. Connect Google and Sync on Sources — takes about a minute.
        </p>
      ) : (
        <ul className={styles.list}>
          {bullets.map((b) => (
            <li key={b.bullet_id}>
              <span className={styles.cat}>{b.category}</span>
              <p>{b.text}</p>
              <div className={styles.row}>
                <button
                  type="button"
                  className={styles.ghost}
                  disabled={busy || b.status === "accepted"}
                  onClick={() => setBullet(b.bullet_id, "accepted")}
                >
                  Keep
                </button>
                <button
                  type="button"
                  className={styles.ghost}
                  disabled={busy}
                  onClick={() => setBullet(b.bullet_id, "rejected")}
                >
                  Not me
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {(profile?.contradictions?.length ?? 0) > 0 && (
        <section className={styles.tensions}>
          <h2>Tensions Level noticed</h2>
          <ul>
            {profile!.contradictions!.map((c) => (
              <li key={c.contradiction_id}>{c.summary}</li>
            ))}
          </ul>
        </section>
      )}

      {bullets.length > 0 && (
        <button type="button" className={styles.primary} disabled={busy} onClick={confirmAll}>
          Looks right
        </button>
      )}

      <section className={styles.chatBlock}>
        <h2>Tell Level more</h2>
        <p className={styles.meta}>
          Example: “I’m always late to my night class” or “Co-parent has the kids every other
          weekend.”
        </p>
        {chat.map((line, i) => (
          <p key={i} className={line.role === "you" ? styles.you : styles.level}>
            {line.text}
          </p>
        ))}
        <form onSubmit={onChat} className={styles.chatForm}>
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={3}
            placeholder="Something true about your life…"
            disabled={busy || !userId}
          />
          <button type="submit" disabled={busy || draft.trim().length < 8}>
            {busy ? "Saving…" : "Save to profile"}
          </button>
        </form>
      </section>

      {status && <p className={styles.status}>{status}</p>}
    </AppShell>
  );
}

export default function ProfilePage() {
  return (
    <Suspense fallback={<AppShell><p className={styles.meta}>Loading…</p></AppShell>}>
      <ProfileInner />
    </Suspense>
  );
}
