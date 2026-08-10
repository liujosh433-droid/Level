"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import {
  DashboardWorkspace,
  TellLevelPanel,
  TellLevelReply,
  TellLevelYou,
} from "@/components/dashboard";
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
  const [displayName, setDisplayName] = useState<string | null>(null);
  const [googleConnected, setGoogleConnected] = useState(false);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [draft, setDraft] = useState("");
  const [chat, setChat] = useState<ChatLine[]>([]);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    void (async () => {
      try {
        const me = await fetchMe();
        setUserId(me.user_id);
        setDisplayName(me.display_name);
        setGoogleConnected(Boolean(me.google_connected));
        setProfile(await fetchProfile());
      } catch (err) {
        if (err instanceof AuthError) {
          router.replace("/welcome");
          return;
        }
        setStatus(err instanceof Error ? err.message : String(err));
      } finally {
        setLoading(false);
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
      setStatus("Priorities saved.");
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function onTellMore(message: string) {
    if (!userId || busy) return;
    setDraft("");
    setChat((prev) => [...prev, { role: "you", text: message }]);
    setBusy(true);
    setStatus(null);
    try {
      const res = await profileChat(message);
      setProfile(res.profile);
      setChat((prev) => [...prev, { role: "level", text: res.reply }]);
    } catch (err) {
      if (err instanceof AuthError) {
        router.replace("/welcome");
        return;
      }
      setStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const bullets = profile?.bullets ?? [];
  const pendingCount = bullets.filter((b) => b.status === "pending").length;

  return (
    <AppShell userId={userId} displayName={displayName} dashboard>
      <DashboardWorkspace
        railAriaLabel="Tell Level more"
        rail={
          <TellLevelPanel
            title="Tell Level more"
            lead="Add a priority Level missed — sleep, family rituals, recovery, or anything that should shape hard decisions."
            placeholder='Ex: “Sunday dinners with my parents are non-negotiable” or “Mental recovery matters more than late work”'
            value={draft}
            onChange={setDraft}
            onSubmit={onTellMore}
            busy={busy}
            disabled={!userId}
            submitLabel="Save priority"
            busyLabel="Saving…"
            minLength={8}
            voiceEnabled
            onVoiceError={setStatus}
            error={null}
          >
            {chat.length > 0
              ? chat.map((line, i) =>
                  line.role === "you" ? (
                    <article key={i}>
                      <TellLevelYou>{line.text}</TellLevelYou>
                    </article>
                  ) : (
                    <article key={i}>
                      <TellLevelReply>{line.text}</TellLevelReply>
                    </article>
                  ),
                )
              : null}
          </TellLevelPanel>
        }
      >
        <h1 className={styles.title}>Your Priorities</h1>
        <p className={styles.sub}>
          Level reads your real week and takes a step further — not just listing events, but what
          they say you protect. Keep what’s true; toss what isn’t.
        </p>

        {profile?.manifesto && <p className={styles.manifesto}>{profile.manifesto}</p>}

        {loading ? (
          <p className={styles.meta}>Loading your priorities…</p>
        ) : bullets.length === 0 ? (
          <p className={styles.meta}>
            {googleConnected ? (
              <>
                Your calendar is connected — refresh in a moment and Level will draft priorities
                from your week (family time, work hours, recovery, and more).
              </>
            ) : (
              <>
                Connect Google on Sources so Level can infer priorities from your real calendar —
                about a minute.
              </>
            )}
          </p>
        ) : (
          <ul className={styles.list}>
            {bullets.map((b) => (
              <li key={b.bullet_id}>
                <span className={styles.cat}>
                  {b.status === "accepted" || b.status === "edited" ? "Kept" : "Priority"}
                </span>
                <p>{b.text}</p>
                <div className={styles.row}>
                  <button
                    type="button"
                    className={styles.ghost}
                    disabled={busy || b.status === "accepted" || b.status === "edited"}
                    onClick={() => void setBullet(b.bullet_id, "accepted")}
                  >
                    Keep
                  </button>
                  <button
                    type="button"
                    className={styles.ghost}
                    disabled={busy}
                    onClick={() => void setBullet(b.bullet_id, "rejected")}
                  >
                    Not me
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}

        {bullets.length > 0 && pendingCount > 0 && (
          <div className={styles.primaryWrap}>
            <button
              type="button"
              className={styles.primary}
              disabled={busy}
              onClick={() => void confirmAll()}
            >
              Looks right
            </button>
          </div>
        )}

        {status ? <p className={styles.status}>{status}</p> : null}
      </DashboardWorkspace>
    </AppShell>
  );
}

export default function ProfilePage() {
  return (
    <Suspense
      fallback={
        <AppShell dashboard>
          <p className={styles.meta}>Loading…</p>
        </AppShell>
      }
    >
      <ProfileInner />
    </Suspense>
  );
}
