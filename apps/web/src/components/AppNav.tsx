"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import AccountMenu from "./AccountMenu";
import { api } from "@/lib/api";
import type { WhoAmI } from "@/lib/types";
import styles from "./AppNav.module.css";

const TABS = [
  { href: "/today", label: "Today" },
  { href: "/week", label: "Week" },
  { href: "/profile", label: "About me" },
  { href: "/contacts", label: "Contacts" },
  { href: "/sources", label: "Sources" },
  { href: "/about", label: "Info" },
] as const;

function pathMatches(path: string | null, href: string): boolean {
  if (!path) return false;
  return path === href || path.startsWith(`${href}/`);
}

export default function AppNav() {
  const pathname = usePathname();
  const [demoScenario, setDemoScenario] = useState<string | null>(null);

  useEffect(() => {
    // Fetch once at mount; subsequent /v1/me updates arrive via the
    // level:whoami custom event that AccountMenu already dispatches.
    void api
      .get<WhoAmI>("/v1/me")
      .then((me) => setDemoScenario(me.demo ? me.demo_scenario ?? "family" : null))
      .catch(() => setDemoScenario(null));
    function onWho(event: Event) {
      const me = (event as CustomEvent<WhoAmI>).detail;
      if (me) setDemoScenario(me.demo ? me.demo_scenario ?? "family" : null);
    }
    window.addEventListener("level:whoami", onWho);
    return () => window.removeEventListener("level:whoami", onWho);
  }, []);

  return (
    <>
      <header className={styles.header}>
        <div className={styles.headerInner}>
          <Link href="/today" className={styles.brand}>
            Level
          </Link>
          {demoScenario ? (
            <span
              className={styles.demoPill}
              title="Signed in as a synthetic demo user. Email sends are previewed; calendar edits are read-only."
              aria-label={`Demo mode: ${demoScenario === "solo" ? "solo caregiver" : "two-parent family"}`}
            >
              Demo mode
            </span>
          ) : null}
          <div className={styles.headerEnd}>
            <nav className={styles.desktopNav} aria-label="Main">
              {TABS.map((tab) => {
                const active = pathMatches(pathname, tab.href);
                return (
                  <Link
                    key={tab.href}
                    href={tab.href}
                    prefetch
                    className={active ? styles.active : undefined}
                    aria-current={active ? "page" : undefined}
                  >
                    {tab.label}
                  </Link>
                );
              })}
            </nav>
            <AccountMenu />
          </div>
        </div>
      </header>

      <nav className={styles.bottomNav} aria-label="Main">
        {TABS.map((tab) => {
          const active = pathMatches(pathname, tab.href);
          return (
            <Link
              key={tab.href}
              href={tab.href}
              prefetch
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
