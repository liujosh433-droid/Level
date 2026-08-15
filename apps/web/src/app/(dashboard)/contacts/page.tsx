"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import {
  AuthError,
  addCarePerson,
  ensureSelfPerson,
  fetchMe,
  fetchProfile,
  savePersonContacts,
  type CareContactView,
  type CarePersonView,
  type Profile,
} from "@/lib/api";
import styles from "./contacts.module.css";

type PersonKind = "self" | "child" | "elder" | "other";

function personKind(person: CarePersonView): PersonKind {
  const role = (person.care_role_id || "child_care").toLowerCase();
  if (role === "self") return "self";
  if (role === "elder_care") return "elder";
  if (role === "child_care") return "child";
  return "other";
}

function defaultRoles(person: CarePersonView): string[] {
  return personKind(person) === "child" ? ["Teacher", "Doctor"] : ["Doctor"];
}

function contactsFor(
  person: CarePersonView,
  drafts: Record<string, CareContactView[]>,
): CareContactView[] {
  if (drafts[person.person_id]) return drafts[person.person_id];
  const saved = person.contacts ?? [];
  if (saved.length > 0) return saved;
  return defaultRoles(person).map((role) => ({
    contact_id: "",
    role,
    name: "",
    email: role.toLowerCase() === "teacher" ? person.teacher_email || "" : "",
  }));
}

function ContactsInner() {
  const router = useRouter();
  const [userId, setUserId] = useState("");
  const [displayName, setDisplayName] = useState<string | null>(null);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [drafts, setDrafts] = useState<Record<string, CareContactView[]>>({});
  const [newChild, setNewChild] = useState("");
  const [newElder, setNewElder] = useState("");
  const [newRole, setNewRole] = useState<Record<string, string>>({});

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const me = await fetchMe();
        await ensureSelfPerson(me.display_name || "You");
        const next = await fetchProfile();
        if (cancelled) return;
        setUserId(me.user_id);
        setDisplayName(me.display_name);
        setProfile(next);
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

  function setContacts(personId: string, rows: CareContactView[]) {
    setDrafts((prev) => ({ ...prev, [personId]: rows }));
  }

  async function saveContacts(person: CarePersonView) {
    if (!userId) return;
    setBusy(true);
    try {
      await savePersonContacts(person.person_id, contactsFor(person, drafts));
      const next = await fetchProfile();
      setProfile(next);
      setDrafts((prev) => {
        const copy = { ...prev };
        delete copy[person.person_id];
        return copy;
      });
      setStatus("Contacts saved.");
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function onAddPerson(rawName: string, careRoleId: "child_care" | "elder_care") {
    const name = rawName.trim();
    if (!name || !userId) return;
    setBusy(true);
    try {
      await addCarePerson(
        name,
        careRoleId === "child_care" ? "child" : "elder",
        careRoleId,
      );
      if (careRoleId === "child_care") setNewChild("");
      else setNewElder("");
      const next = await fetchProfile();
      setProfile(next);
      setStatus(
        careRoleId === "child_care"
          ? "Added. Fill in their teacher or doctor below."
          : "Added. Fill in their doctor below.",
      );
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const people = profile?.people ?? [];
  const you = people.filter((p) => personKind(p) === "self");
  const kids = people.filter((p) => personKind(p) === "child");
  const elders = people.filter((p) => personKind(p) === "elder");
  const others = people.filter((p) => personKind(p) === "other");

  function renderPerson(person: CarePersonView) {
    const rows = contactsFor(person, drafts);
    const kind = personKind(person);
    const title =
      kind === "self"
        ? person.display_name && person.display_name.toLowerCase() !== "you"
          ? person.display_name
          : displayName || "You"
        : person.display_name;
    return (
      <li key={person.person_id}>
        <span className={styles.cat}>{title}</span>
        {rows.map((row, idx) => (
          <div key={`${row.contact_id}-${idx}`} className={styles.contactRow}>
            <input
              className={styles.field}
              placeholder="Type"
              value={row.role}
              onChange={(e) =>
                setContacts(
                  person.person_id,
                  rows.map((r, i) => (i === idx ? { ...r, role: e.target.value } : r)),
                )
              }
            />
            <input
              className={styles.field}
              placeholder="Name"
              value={row.name}
              onChange={(e) =>
                setContacts(
                  person.person_id,
                  rows.map((r, i) => (i === idx ? { ...r, name: e.target.value } : r)),
                )
              }
            />
            <input
              className={`${styles.field} ${styles.email}`}
              type="email"
              placeholder="Email"
              value={row.email}
              onChange={(e) =>
                setContacts(
                  person.person_id,
                  rows.map((r, i) => (i === idx ? { ...r, email: e.target.value } : r)),
                )
              }
            />
          </div>
        ))}
        <div className={styles.addRole}>
          <input
            className={styles.field}
            placeholder="Add another type"
            value={newRole[person.person_id] ?? ""}
            onChange={(e) =>
              setNewRole((prev) => ({
                ...prev,
                [person.person_id]: e.target.value,
              }))
            }
          />
          <button
            type="button"
            className={styles.save}
            disabled={busy || !(newRole[person.person_id] || "").trim()}
            onClick={() => {
              const role = (newRole[person.person_id] || "").trim();
              if (!role) return;
              setContacts(person.person_id, [
                ...rows,
                { contact_id: "", role, name: "", email: "" },
              ]);
              setNewRole((prev) => ({ ...prev, [person.person_id]: "" }));
            }}
          >
            Add type
          </button>
        </div>
        <div className={styles.row}>
          <button
            type="button"
            className={styles.save}
            disabled={busy}
            onClick={() => void saveContacts(person)}
          >
            Save contacts
          </button>
        </div>
      </li>
    );
  }

  return (
    <AppShell userId={userId} displayName={displayName} dashboard contentOnly>
      <div className={styles.page}>
      <h1 className={styles.title}>Contacts</h1>
      <p className={styles.sub}>
        People you hold, and who Level can email for them. Kids have a teacher and
        doctor; you and elders have a doctor — add any other type you need. Say
        “email her teacher” on Today and Level shows a preview before it sends.
      </p>

      {loading ? (
        <p className={styles.meta}>Loading contacts…</p>
      ) : (
        <>
          <section className={styles.section}>
            <h2 className={styles.sectionTitle}>You</h2>
            <ul className={styles.list}>{you.map(renderPerson)}</ul>
          </section>

          <section className={styles.section}>
            <h2 className={styles.sectionTitle}>Kids</h2>
            {kids.length === 0 ? (
              <p className={styles.meta}>No kids listed yet — add a name below.</p>
            ) : (
              <ul className={styles.list}>{kids.map(renderPerson)}</ul>
            )}
            <div className={styles.addPerson}>
              <input
                className={styles.field}
                placeholder="Add a child (name)"
                value={newChild}
                onChange={(e) => setNewChild(e.target.value)}
                disabled={busy}
              />
              <button
                type="button"
                className={styles.save}
                disabled={busy || !newChild.trim()}
                onClick={() => void onAddPerson(newChild, "child_care")}
              >
                Add
              </button>
            </div>
          </section>

          <section className={styles.section}>
            <h2 className={styles.sectionTitle}>Elder care</h2>
            {elders.length === 0 ? (
              <p className={styles.meta}>No one in elder care yet — add a name below.</p>
            ) : (
              <ul className={styles.list}>{elders.map(renderPerson)}</ul>
            )}
            <div className={styles.addPerson}>
              <input
                className={styles.field}
                placeholder="Add someone in elder care (name)"
                value={newElder}
                onChange={(e) => setNewElder(e.target.value)}
                disabled={busy}
              />
              <button
                type="button"
                className={styles.save}
                disabled={busy || !newElder.trim()}
                onClick={() => void onAddPerson(newElder, "elder_care")}
              >
                Add
              </button>
            </div>
          </section>

          {others.length > 0 ? (
            <section className={styles.section}>
              <h2 className={styles.sectionTitle}>Others</h2>
              <ul className={styles.list}>{others.map(renderPerson)}</ul>
            </section>
          ) : null}
        </>
      )}

      {status ? <p className={styles.status}>{status}</p> : null}
      </div>
    </AppShell>
  );
}

export default function ContactsPage() {
  return (
    <Suspense
      fallback={
        <AppShell dashboard contentOnly>
          <div className={styles.page}>
            <p className={styles.meta}>Loading…</p>
          </div>
        </AppShell>
      }
    >
      <ContactsInner />
    </Suspense>
  );
}
