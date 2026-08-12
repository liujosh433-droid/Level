"use client";

import { ReactNode, useEffect, useState } from "react";
import { AppNav } from "./AppNav";
import { fetchMe } from "@/lib/api";
import styles from "./AppShell.module.css";

export function AppShell({
  userId,
  displayName: displayNameProp,
  children,
  wide,
  dashboard,
  /** When true, render only page body (nav/shell provided by dashboard layout). */
  contentOnly,
}: {
  /** When set, shows signed-in chrome. Identity comes from the cookie. */
  userId?: string;
  displayName?: string | null;
  children: ReactNode;
  wide?: boolean;
  /** Two-column dashboard workspace (Today, Profile, …). */
  dashboard?: boolean;
  contentOnly?: boolean;
}) {
  const [displayName, setDisplayName] = useState<string | null | undefined>(displayNameProp);

  useEffect(() => {
    setDisplayName(displayNameProp);
  }, [displayNameProp]);

  useEffect(() => {
    if (!userId || displayNameProp) return;
    void fetchMe()
      .then((me) => setDisplayName(me.display_name))
      .catch(() => undefined);
  }, [userId, displayNameProp]);

  if (contentOnly) {
    return <div className={styles.body}>{children}</div>;
  }

  const shellClass = [
    styles.shell,
    wide ? styles.wide : "",
    dashboard ? styles.dashboard : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={styles.root}>
      <AppNav
        signedIn={Boolean(userId)}
        displayName={displayName}
        onDisplayNameChange={setDisplayName}
      />
      <div className={shellClass}>
        <div className={styles.body}>{children}</div>
      </div>
    </div>
  );
}
