"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { api, ApiError, type Session } from "@/lib/api";

export const errMsg = (e: unknown) =>
  e instanceof ApiError ? e.message : "Something went wrong. Try again.";

/** Loads the session for a dashboard page; a 401 bounces to login with `next` set. */
export function useSession(nextPath: string) {
  const router = useRouter();
  const [session, setSession] = useState<Session | null>(null);

  const loadSession = useCallback(async (): Promise<Session | null> => {
    try {
      const me = await api.me();
      setSession(me);
      return me;
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        router.replace(`/login?next=${encodeURIComponent(nextPath)}`);
        return null;
      }
      throw err;
    }
  }, [router, nextPath]);

  useEffect(() => {
    void loadSession().catch(() => undefined);
  }, [loadSession]);

  return { session, loadSession };
}
