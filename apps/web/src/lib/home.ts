import { api, ApiError } from "./api";
import type { WhoAmI } from "./types";

export type HomeDestination = "/today" | "/welcome";

export async function resolveHomeDestination(): Promise<{ dest: HomeDestination }> {
  try {
    const me = await api.get<WhoAmI>("/v1/me");
    if (me.google_connected) return { dest: "/today" };
    return { dest: "/welcome" };
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return { dest: "/welcome" };
    return { dest: "/welcome" };
  }
}
