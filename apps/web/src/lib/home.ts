import { AuthError, fetchMe, type Me } from "@/lib/api";

export type HomeDest = "/today" | "/sources" | "/welcome";

/** Where a visitor should land based on the current session cookie. */
export async function resolveHomeDestination(): Promise<{
  dest: HomeDest;
  me: Me | null;
}> {
  try {
    const me = await fetchMe();
    if (me.google_connected) {
      return { dest: "/today", me };
    }
    // Session exists but Calendar isn't linked yet.
    return { dest: "/sources", me };
  } catch (err) {
    if (err instanceof AuthError) {
      return { dest: "/welcome", me: null };
    }
    return { dest: "/welcome", me: null };
  }
}
