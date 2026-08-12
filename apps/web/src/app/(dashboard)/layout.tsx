"use client";

import { ReactNode, useEffect, useState } from "react";
import { AppNav } from "@/components/AppNav";
import { fetchMe } from "@/lib/api";
import styles from "@/components/AppShell.module.css";

/**
 * Persistent chrome for Today / About me / Sources / Info.
 * Keeps nav + page background mounted so route changes don't flash the shell.
 */
export default function DashboardLayout({ children }: { children: ReactNode }) {
  const [userId, setUserId] = useState<string | undefined>();
  const [displayName, setDisplayName] = useState<string | null | undefined>();

  useEffect(() => {
    void fetchMe()
      .then((me) => {
        setUserId(me.user_id);
        setDisplayName(me.display_name);
      })
      .catch(() => {
        setUserId(undefined);
        setDisplayName(undefined);
      });
  }, []);

  return (
    <div className={styles.root}>
      <AppNav
        signedIn={Boolean(userId)}
        displayName={displayName}
        onDisplayNameChange={setDisplayName}
      />
      <div className={`${styles.shell} ${styles.dashboard}`}>
        <div className={styles.body}>{children}</div>
      </div>
    </div>
  );
}
