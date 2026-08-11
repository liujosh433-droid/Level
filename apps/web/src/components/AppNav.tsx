"use client";

import Link from "next/link";
import { FormEvent, useEffect, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { logout, updateDisplayName } from "@/lib/api";
import styles from "./AppNav.module.css";

const TABS = [
  { href: "/today", label: "Today", hint: "Schedule & ask" },
  { href: "/profile", label: "About me", hint: "Who you are" },
  { href: "/sources", label: "Sources", hint: "Calendar & Memory" },
  { href: "/about", label: "Info", hint: "What Level can do" },
] as const;

function initials(name?: string | null): string {
  const raw = (name || "").trim();
  if (!raw || ["guest parent", "caregiver", "guest"].includes(raw.toLowerCase())) {
    return "L";
  }
  const parts = raw.split(/\s+/).slice(0, 2);
  return parts.map((p) => p[0]?.toUpperCase() ?? "").join("") || "L";
}

function pathMatches(path: string, href: string): boolean {
  return path === href || path.startsWith(`${href}/`);
}

export function AppNav({
  signedIn,
  displayName,
  onDisplayNameChange,
}: {
  signedIn?: boolean;
  displayName?: string | null;
  onDisplayNameChange?: (name: string) => void;
}) {
  const pathname = usePathname();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [nameDraft, setNameDraft] = useState(displayName || "");
  /** Highlight the clicked tab immediately — don’t wait for the route to finish loading. */
  const [pendingPath, setPendingPath] = useState<string | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  const activePath = pendingPath ?? pathname;

  useEffect(() => {
    setNameDraft(displayName || "");
  }, [displayName]);

  useEffect(() => {
    setPendingPath(null);
  }, [pathname]);

  useEffect(() => {
    for (const tab of TABS) {
      router.prefetch(tab.href);
    }
  }, [router]);

  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (!menuRef.current?.contains(e.target as Node)) {
        setOpen(false);
        setEditing(false);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        setOpen(false);
        setEditing(false);
      }
    }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  async function onLogout() {
    if (busy) return;
    setBusy(true);
    try {
      await logout();
      router.replace("/");
    } finally {
      setBusy(false);
      setOpen(false);
    }
  }

  async function onSaveName(e: FormEvent) {
    e.preventDefault();
    const next = nameDraft.trim();
    if (!next || busy) return;
    setBusy(true);
    try {
      const me = await updateDisplayName(next);
      onDisplayNameChange?.(me.display_name || next);
      setEditing(false);
    } catch {
      // keep draft; user can retry
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <header className={styles.header}>
        <div className={styles.headerInner}>
          <Link href={signedIn ? "/today" : "/"} className={styles.brand}>
            Level
          </Link>
          <div className={styles.right}>
            <nav className={styles.desktopNav} aria-label="Main">
              {TABS.map((tab) => {
                const active = pathMatches(activePath, tab.href);
                return (
                  <Link
                    key={tab.href}
                    href={tab.href}
                    prefetch
                    className={active ? styles.active : undefined}
                    title={tab.hint}
                    onClick={() => setPendingPath(tab.href)}
                  >
                    {tab.label}
                  </Link>
                );
              })}
            </nav>
            {signedIn ? (
              <div className={styles.avatarWrap} ref={menuRef}>
                <button
                  type="button"
                  className={styles.avatar}
                  aria-haspopup="menu"
                  aria-expanded={open}
                  aria-label="Account menu"
                  onClick={() => setOpen((v) => !v)}
                >
                  {initials(displayName)}
                </button>
                {open && (
                  <div className={styles.menu} role="menu">
                    <div className={styles.menuHead}>
                      <p className={styles.menuLabel}>Signed in as</p>
                      {!editing ? (
                        <button
                          type="button"
                          className={styles.menuName}
                          onClick={() => setEditing(true)}
                        >
                          {(displayName || "Add your name").trim()}
                          <span>Edit</span>
                        </button>
                      ) : (
                        <form onSubmit={onSaveName} className={styles.nameForm}>
                          <input
                            value={nameDraft}
                            onChange={(e) => setNameDraft(e.target.value)}
                            maxLength={80}
                            autoFocus
                            placeholder="Your name"
                            disabled={busy}
                          />
                          <button type="submit" disabled={busy || !nameDraft.trim()}>
                            Save
                          </button>
                        </form>
                      )}
                    </div>
                    <Link
                      href="/profile"
                      className={styles.menuItem}
                      role="menuitem"
                      onClick={() => {
                        setPendingPath("/profile");
                        setOpen(false);
                      }}
                    >
                      About me
                    </Link>
                    <Link
                      href="/about"
                      className={styles.menuItem}
                      role="menuitem"
                      onClick={() => {
                        setPendingPath("/about");
                        setOpen(false);
                      }}
                    >
                      Info
                    </Link>
                    <button
                      type="button"
                      className={styles.menuItemDanger}
                      role="menuitem"
                      onClick={() => void onLogout()}
                      disabled={busy}
                    >
                      {busy ? "…" : "Log out"}
                    </button>
                  </div>
                )}
              </div>
            ) : null}
          </div>
        </div>
      </header>

      <nav className={styles.bottomNav} aria-label="Main">
        {TABS.map((tab) => {
          const active = pathMatches(activePath, tab.href);
          return (
            <Link
              key={tab.href}
              href={tab.href}
              prefetch
              className={active ? `${styles.tab} ${styles.tabActive}` : styles.tab}
              aria-current={active ? "page" : undefined}
              onClick={() => setPendingPath(tab.href)}
            >
              <span className={styles.tabDot} aria-hidden="true" />
              {tab.label}
            </Link>
          );
        })}
      </nav>
    </>
  );
}
