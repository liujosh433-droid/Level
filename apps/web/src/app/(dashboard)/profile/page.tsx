"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import {
  CareLoadGraph,
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

const CARE_ROLE_LABELS: Record<string, string> = {
  child_care: "Child care",
  elder_care: "Elder care",
  paid_work: "Work/Job",
  self_recovery: "Self & recovery",
  household_logistics: "Household logistics",
  partner_coparent: "Co-parent / partner",
};

function careRoleLabel(id?: string | null): string | null {
  if (!id) return null;
  return CARE_ROLE_LABELS[id] ?? id.replace(/_/g, " ");
}

function formatCareUpdated(iso: string): string {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function ManifestoSummary({
  text,
  fallbackItems,
}: {
  text: string;
  fallbackItems: string[];
}) {
  const lines = text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
  let bullets = lines
    .filter((l) => l.startsWith("•") || l.startsWith("-"))
    .map((l) => l.replace(/^[•\-]\s*/, ""));
  // Older paragraph-style manifesto → use priority bullets instead of one long line.
  if (bullets.length === 0 && fallbackItems.length > 0) {
    bullets = fallbackItems.slice(0, 3);
  }
  const intro =
    lines.find((l) => !l.startsWith("•") && !l.startsWith("-") && !l.includes(" — ")) ||
    "Right now it looks like you prioritize";

  if (bullets.length === 0) {
    return <p className={styles.manifesto}>{text}</p>;
  }

  return (
    <div className={styles.manifesto}>
      <p className={styles.manifestoIntro}>{intro.replace(/:$/, "")}</p>
      <ul className={styles.manifestoList}>
        {bullets.map((b) => (
          <li key={b}>{b}</li>
        ))}
      </ul>
    </div>
  );
}

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
    let cancelled = false;
    void (async () => {
      try {
        // Navigate paints first; identity + profile load together in the background.
        const [me, nextProfile] = await Promise.all([fetchMe(), fetchProfile()]);
        if (cancelled) return;
        setUserId(me.user_id);
        setDisplayName(me.display_name);
        setGoogleConnected(Boolean(me.google_connected));
        setProfile(nextProfile);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof AuthError) {
          router.replace("/welcome");
          return;
        }
        setStatus(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
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
      setStatus("Saved.");
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
    <AppShell userId={userId} displayName={displayName} dashboard contentOnly>
      <DashboardWorkspace
        railAriaLabel="Tell Level more"
        rail={
          <div className={styles.railStack}>
            <div className={styles.chatBlock}>
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
                stickyInput={false}
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
            </div>
            <div className={styles.graphBlock}>
              <CareLoadGraph graph={profile?.care_graph} />
            </div>
          </div>
        }
      >
        <h1 className={styles.title}>About me</h1>
        <p className={styles.sub}>
          What Level has gathered about you — care load, preferences, and how you like to be
          helped — grounded only in what you’ve shared or connected.
        </p>

        {profile?.about_summary ? (
          <section className={styles.aboutSummary} aria-label="Level’s read on you">
            <p className={styles.aboutKicker}>Level’s read</p>
            <p className={styles.aboutBody}>{profile.about_summary}</p>
          </section>
        ) : null}

        {profile &&
        (profile.care_profile_version != null || profile.care_role_count) ? (
          <p className={styles.versionLine}>
            Care Profile v{profile.care_profile_version ?? "—"}
            {profile.care_role_count != null
              ? ` · ${profile.care_role_count} role${profile.care_role_count === 1 ? "" : "s"}`
              : ""}
            {profile.care_updated_at
              ? ` · updated ${formatCareUpdated(profile.care_updated_at)}`
              : ""}
            {typeof profile.fact_count === "number"
              ? ` · ${profile.fact_count} facts`
              : ""}
          </p>
        ) : null}

        {profile?.conflict_summaries && profile.conflict_summaries.length > 0 ? (
          <p className={styles.conflictLine}>
            <span className={styles.conflictLabel}>Heads up</span>
            {profile.conflict_summaries[0]}
          </p>
        ) : null}

        {profile?.manifesto || bullets.length > 0 ? (
          <ManifestoSummary
            text={profile?.manifesto || ""}
            fallbackItems={bullets.map((b) => b.text)}
          />
        ) : null}

        {loading ? (
          <p className={styles.meta}>Loading about you…</p>
        ) : bullets.length === 0 ? (
          <p className={styles.meta}>
            {googleConnected ? (
              <>
                Your calendar is connected — refresh in a moment and Level will draft what it
                knows from your week (care roles, people, and load).
              </>
            ) : (
              <>
                Connect Google on Sources so Level can learn from your real calendar —
                about a minute.
              </>
            )}
          </p>
        ) : (
          <>
            <h2 className={styles.sectionTitle}>Care load</h2>
            <ul className={styles.list}>
            {bullets.map((b) => (
              <li key={b.bullet_id}>
                <span className={styles.cat}>
                  {careRoleLabel(b.care_role_id) || "Care role"}
                  {b.status === "accepted" || b.status === "edited" ? " · Holding" : ""}
                </span>
                <p>{b.text}</p>
                <div className={styles.row}>
                  <button
                    type="button"
                    className={styles.keep}
                    disabled={busy || b.status === "accepted" || b.status === "edited"}
                    onClick={() => void setBullet(b.bullet_id, "accepted")}
                  >
                    Keep
                  </button>
                  <button
                    type="button"
                    className={styles.notMe}
                    disabled={busy}
                    onClick={() => void setBullet(b.bullet_id, "rejected")}
                  >
                    Not me
                  </button>
                </div>
              </li>
            ))}
          </ul>
          </>
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
        <AppShell dashboard contentOnly>
          <p className={styles.meta}>Loading…</p>
        </AppShell>
      }
    >
      <ProfileInner />
    </Suspense>
  );
}
