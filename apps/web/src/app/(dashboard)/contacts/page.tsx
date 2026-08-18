"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import type { CarePerson, Contact } from "@/lib/types";
import styles from "./contacts.module.css";

type ContactsResp = { contacts: Contact[] };
type ProfileResp = { people: CarePerson[] };

const KIND_OPTIONS: Contact["kind"][] = ["teacher", "doctor", "coach", "other"];

const KIND_ICON: Record<Contact["kind"], string> = {
  teacher: "\u{1F3EB}",
  doctor: "\u{1FA7A}",
  coach: "\u{1F3C6}",
  other: "\u{1F464}",
};

type DraftRow = {
  contact_id: string | null;
  kind: Contact["kind"];
  name: string;
  email: string;
  phone: string;
};

function defaultRowsForPerson(person: CarePerson): DraftRow[] {
  if (person.is_self) return [{ contact_id: null, kind: "doctor", name: "", email: "", phone: "" }];
  if (person.relation === "child") {
    return [
      { contact_id: null, kind: "teacher", name: "", email: "", phone: "" },
      { contact_id: null, kind: "doctor", name: "", email: "", phone: "" },
    ];
  }
  return [{ contact_id: null, kind: "doctor", name: "", email: "", phone: "" }];
}

function toDraftRows(existing: Contact[]): DraftRow[] {
  return existing.map((c) => ({
    contact_id: c.contact_id,
    kind: c.kind,
    name: c.name,
    email: c.email ?? "",
    phone: c.phone ?? "",
  }));
}

const PERSON_GROUPS: {
  key: string;
  title: string;
  relations: string[];
  isSelf?: boolean;
  addLabel?: string;
}[] = [
  { key: "self", title: "You", relations: [], isSelf: true },
  { key: "child", title: "Kids", relations: ["child"], addLabel: "Add a child" },
  { key: "elder", title: "Elder care", relations: ["elder"], addLabel: "Add someone in elder care" },
  { key: "partner", title: "Partner", relations: ["partner"], addLabel: "Add partner" },
  { key: "other", title: "Others", relations: ["friend", "other"] },
];

export default function ContactsPage() {
  const [people, setPeople] = useState<CarePerson[]>([]);
  const [drafts, setDrafts] = useState<Record<string, DraftRow[]>>({});
  const [savedIndicator, setSavedIndicator] = useState<Record<string, boolean>>({});
  const [busyPerson, setBusyPerson] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [adding, setAdding] = useState<Record<string, string>>({});
  const [addBusy, setAddBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [profile, contactsResp] = await Promise.all([
        api.get<ProfileResp>("/v1/profile"),
        api.get<ContactsResp>("/v1/contacts"),
      ]);
      const kept = profile.people.filter((p) => p.status !== "not_me");
      setPeople(kept);
      const byPerson = new Map<string, Contact[]>();
      for (const c of contactsResp.contacts) {
        const arr = byPerson.get(c.person_id) ?? [];
        arr.push(c);
        byPerson.set(c.person_id, arr);
      }
      const nextDrafts: Record<string, DraftRow[]> = {};
      for (const person of kept) {
        const existing = byPerson.get(person.person_id) ?? [];
        nextDrafts[person.person_id] =
          existing.length > 0 ? toDraftRows(existing) : defaultRowsForPerson(person);
      }
      setDrafts(nextDrafts);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const grouped = useMemo(() => {
    return PERSON_GROUPS.map((group) => ({
      ...group,
      people: people.filter((p) =>
        group.isSelf
          ? p.is_self
          : !p.is_self && group.relations.includes(p.relation),
      ),
    }));
  }, [people]);

  function updateRow(personId: string, idx: number, patch: Partial<DraftRow>) {
    setDrafts((prev) => {
      const rows = prev[personId] ?? [];
      const next = rows.map((r, i) => (i === idx ? { ...r, ...patch } : r));
      return { ...prev, [personId]: next };
    });
    setSavedIndicator((s) => ({ ...s, [personId]: false }));
  }

  function addRow(personId: string) {
    setDrafts((prev) => {
      const rows = prev[personId] ?? [];
      const nextKind = KIND_OPTIONS.find((k) => !rows.some((r) => r.kind === k)) ?? "other";
      return {
        ...prev,
        [personId]: [...rows, { contact_id: null, kind: nextKind, name: "", email: "", phone: "" }],
      };
    });
  }

  function removeRow(personId: string, idx: number) {
    setDrafts((prev) => {
      const rows = prev[personId] ?? [];
      return { ...prev, [personId]: rows.filter((_, i) => i !== idx) };
    });
    setSavedIndicator((s) => ({ ...s, [personId]: false }));
  }

  async function save(person: CarePersonRow) {
    setBusyPerson(person.person_id);
    setStatus(null);
    try {
      const rows = drafts[person.person_id] ?? [];
      for (const row of rows) {
        const trimmedName = row.name.trim();
        if (!trimmedName && !row.email.trim() && !row.phone.trim()) continue;
        await api.post("/v1/contacts", {
          contact_id: row.contact_id ?? undefined,
          person_id: person.person_id,
          kind: row.kind,
          name: trimmedName || "Untitled",
          email: row.email.trim() || null,
          phone: row.phone.trim() || null,
          notes: "",
        });
      }
      setSavedIndicator((s) => ({ ...s, [person.person_id]: true }));
      setStatus(`Saved contacts for ${person.display_name}.`);
      await load();
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyPerson(null);
    }
  }

  async function addPerson(relation: string) {
    const raw = (adding[relation] ?? "").trim();
    if (!raw) return;
    setAddBusy(true);
    setStatus(null);
    try {
      await api.post("/v1/profile/people", {
        display_name: raw,
        relation,
        is_self: false,
      });
      setAdding((prev) => ({ ...prev, [relation]: "" }));
      await load();
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setAddBusy(false);
    }
  }

  if (loading) {
    return <p className={styles.meta}>Loading contacts&hellip;</p>;
  }

  return (
    <div className={styles.wrap}>
      <header className={styles.header}>
        <h1>Contacts</h1>
        <p className={styles.sub}>
          The people Level can email on your behalf. Kids get a teacher and a
          doctor by default &mdash; add any other role you need. On Today, say
          &ldquo;email Nova&rsquo;s teacher&rdquo; and Level drafts a preview
          in chat. If there are two, it will ask which one.
        </p>
      </header>

      {status ? <p className={styles.status}>{status}</p> : null}

      {grouped.map((group) => (
        <section key={group.key} className={styles.section}>
          <h2 className={styles.sectionTitle}>{group.title}</h2>

          {group.people.length === 0 && !group.isSelf ? (
            <p className={styles.meta}>
              No one here yet.
              {group.addLabel ? " Add someone below." : ""}
            </p>
          ) : (
            <ul className={styles.personList}>
              {group.people.map((person) => (
                <li key={person.person_id} className={styles.personCard}>
                  <div className={styles.personHead}>
                    <div>
                      <p className={styles.personName}>{person.display_name}</p>
                      <p className={styles.personRel}>{group.isSelf ? "you" : person.relation}</p>
                    </div>
                    <div className={styles.personActions}>
                      {savedIndicator[person.person_id] ? (
                        <span className={styles.savedBadge}>Saved</span>
                      ) : null}
                      <button
                        type="button"
                        className="button-primary"
                        onClick={() => void save(person)}
                        disabled={busyPerson === person.person_id}
                      >
                        {busyPerson === person.person_id ? "Saving\u2026" : "Save contacts"}
                      </button>
                    </div>
                  </div>

                  <ul className={styles.contactRows}>
                    {(drafts[person.person_id] ?? []).map((row, idx) => (
                      <li key={`${person.person_id}-${idx}`} className={styles.contactRow}>
                        <label className={styles.field}>
                          <span className={styles.fieldLabel}>Type</span>
                          <div className={styles.kindPicker}>
                            <span className={styles.kindIcon} aria-hidden="true">
                              {KIND_ICON[row.kind]}
                            </span>
                            <select
                              value={row.kind}
                              onChange={(e) =>
                                updateRow(person.person_id, idx, {
                                  kind: e.target.value as Contact["kind"],
                                })
                              }
                            >
                              {KIND_OPTIONS.map((k) => (
                                <option key={k} value={k}>
                                  {k[0].toUpperCase() + k.slice(1)}
                                </option>
                              ))}
                            </select>
                          </div>
                        </label>
                        <label className={styles.field}>
                          <span className={styles.fieldLabel}>Name</span>
                          <input
                            type="text"
                            value={row.name}
                            placeholder="e.g. Ms. Chen"
                            onChange={(e) => updateRow(person.person_id, idx, { name: e.target.value })}
                          />
                        </label>
                        <label className={styles.field}>
                          <span className={styles.fieldLabel}>Email</span>
                          <input
                            type="email"
                            value={row.email}
                            placeholder="name@school.edu"
                            onChange={(e) => updateRow(person.person_id, idx, { email: e.target.value })}
                          />
                        </label>
                        <label className={styles.field}>
                          <span className={styles.fieldLabel}>Phone</span>
                          <input
                            type="tel"
                            value={row.phone}
                            placeholder="Optional"
                            onChange={(e) => updateRow(person.person_id, idx, { phone: e.target.value })}
                          />
                        </label>
                        <button
                          type="button"
                          className={styles.removeBtn}
                          onClick={() => removeRow(person.person_id, idx)}
                          aria-label="Remove this contact row"
                          title="Remove"
                        >
                          &times;
                        </button>
                      </li>
                    ))}
                  </ul>

                  <button
                    type="button"
                    className={styles.addRowBtn}
                    onClick={() => addRow(person.person_id)}
                  >
                    + Add another type
                  </button>
                </li>
              ))}
            </ul>
          )}

          {group.addLabel ? (
            <div className={styles.addPerson}>
              <input
                type="text"
                placeholder={group.addLabel}
                value={adding[group.relations[0]] ?? ""}
                onChange={(e) =>
                  setAdding((prev) => ({ ...prev, [group.relations[0]]: e.target.value }))
                }
                disabled={addBusy}
              />
              <button
                type="button"
                className="button-ghost"
                disabled={addBusy || !(adding[group.relations[0]] ?? "").trim()}
                onClick={() => void addPerson(group.relations[0])}
              >
                Add
              </button>
            </div>
          ) : null}
        </section>
      ))}
    </div>
  );
}

type CarePersonRow = CarePerson;
