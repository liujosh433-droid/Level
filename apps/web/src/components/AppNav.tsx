"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";
import { logout } from "@/lib/api";
import styles from "./AppNav.module.css";

const TABS = [
  { href: "/today", label: "Today", hint: "Schedule & ask" },
  { href: "/profile", label: "Profile", hint: "Who you are" },
  { href: "/sources", label: "Sources", hint: "Connect & sync" },
] as const;

export function AppNav({ signedIn }: { signedIn?: boolean }) {
  const pathname = usePathname();
  const router = useRouter();
  const [busy, setBusy] = useState(false);

  async function onLogout() {
    if (busy) return;
    setBusy(true);
    try {
      await logout();
      router.replace("/");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <header className={styles.header}>
        <Link href={signedIn ? "/today" : "/"} className={styles.brand}>
          Level
        </Link>
        <div className={styles.right}>
          <nav className={styles.desktopNav} aria-label="Main">
            {TABS.map((tab) => {
              const active = pathname === tab.href || pathname.startsWith(`${tab.href}/`);
              return (
                <Link
                  key={tab.href}
                  href={tab.href}
                  className={active ? styles.active : undefined}
                  title={tab.hint}
                >
                  {tab.label}
                </Link>
              );
            })}
          </nav>
          {signedIn ? (
            <button
              type="button"
              className={styles.logout}
              onClick={onLogout}
              disabled={busy}
            >
              {busy ? "…" : "Log out"}
            </button>
          ) : null}
        </div>
      </header>

      <nav className={styles.bottomNav} aria-label="Main">
        {TABS.map((tab) => {
          const active = pathname === tab.href || pathname.startsWith(`${tab.href}/`);
          return (
            <Link
              key={tab.href}
              href={tab.href}
              className={active ? `${styles.tab} ${styles.tabActive}` : styles.tab}
              aria-current={active ? "page" : undefined}
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
