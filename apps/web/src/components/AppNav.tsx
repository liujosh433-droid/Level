"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import styles from "./AppNav.module.css";

const TABS = [
  { href: "/today", label: "Today" },
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
  return (
    <>
      <header className={styles.header}>
        <div className={styles.headerInner}>
          <Link href="/today" className={styles.brand}>
            Level
          </Link>
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
