import { api, ApiError } from "./api";
import type { WhoAmI } from "./types";

/**
 * Resolve the destination the home page should redirect to.
 *
 * ``"today"`` means: user is authenticated + connected, send them
 * straight to the dashboard. ``"landing"`` means: leave them on the
 * marketing hero at ``/`` so they can hit Get Started / Demo.
 *
 * Historical note: this used to return literal paths, including a
 * ``/welcome`` route that never existed. Callers now branch on the
 * enum so a future rename of ``/today`` doesn't require touching
 * every consumer.
 */
export type HomeDestination = "today" | "landing";

export async function resolveHomeDestination(): Promise<{ dest: HomeDestination }> {
  try {
    const me = await api.get<WhoAmI>("/v1/me");
    if (me.google_connected) return { dest: "today" };
    return { dest: "landing" };
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return { dest: "landing" };
    return { dest: "landing" };
  }
}
