/**
 * Who is signed in, for the rest of the application.
 *
 * The interesting part is what happens on load. The access token lives in
 * memory, so a reload has none -- but the refresh token is in storage, so the
 * session is not over. The provider therefore starts in a third state that is
 * neither signed in nor signed out: `checking`, during which it exchanges the
 * stored token for a new pair.
 *
 * Skipping that state is the bug this shape exists to avoid. A provider with
 * only two states shows the sign-in screen for a moment on every reload, and
 * the user watches their session apparently end and then un-end.
 *
 * Signing out is also two things, and both are needed. Revoking the refresh
 * token at the API is what makes it stop working; clearing local storage is
 * what makes this browser forget it. Doing only the second leaves a working
 * credential in anything that copied it, which is a sign-out that only looks
 * like one.
 */

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { api } from "./api/client";
import {
  endSession,
  forget,
  hasStoredSession,
  refreshSession,
  remember,
  storedRefreshToken,
  whenSignedOut,
} from "./api/session";
import type { Account } from "./api/types";

type Status = "checking" | "signed-in" | "signed-out";

interface Session {
  status: Status;
  account: Account | null;
  signIn: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
}

const SessionContext = createContext<Session | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status>(() =>
    hasStoredSession() ? "checking" : "signed-out",
  );
  const [account, setAccount] = useState<Account | null>(null);
  const queries = useQueryClient();

  const adopt = useCallback(async () => {
    const me = await api.me();
    setAccount(me);
    setStatus("signed-in");
  }, []);

  useEffect(() => {
    // The session module ends a session when a refresh is refused, which can
    // happen inside any request. This is how that reaches the interface --
    // otherwise the tokens are gone and the screen still says signed in.
    whenSignedOut(() => {
      setAccount(null);
      setStatus("signed-out");
      // Cached research belongs to the account that was signed in. Leaving it
      // would show one person's history to whoever signs in next on this
      // machine, which is the same leak the backend refuses to make.
      queries.clear();
    });
  }, [queries]);

  useEffect(() => {
    if (status !== "checking") return;

    let cancelled = false;
    void (async () => {
      const renewed = await refreshSession();
      if (cancelled) return;
      if (renewed === null) {
        setStatus("signed-out");
        return;
      }
      try {
        await adopt();
      } catch {
        if (!cancelled) setStatus("signed-out");
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [status, adopt]);

  const signIn = useCallback(
    async (email: string, password: string) => {
      remember(await api.login(email, password));
      await adopt();
    },
    [adopt],
  );

  const register = useCallback(
    async (email: string, password: string) => {
      // Registration returns a signed-in session. The password was just
      // verified by being chosen; asking for it again proves nothing.
      remember(await api.register(email, password));
      await adopt();
    },
    [adopt],
  );

  const signOut = useCallback(async () => {
    const token = storedRefreshToken();
    if (token) {
      // Best effort. If the API cannot be reached, the local session is still
      // cleared -- a sign-out that fails because the network is down is not a
      // sign-out the user wants to retry.
      await api.logout(token).catch(() => undefined);
    }
    forget();
    setAccount(null);
    setStatus("signed-out");
    queries.clear();
  }, [queries]);

  return (
    <SessionContext.Provider value={{ status, account, signIn, register, signOut }}>
      {children}
    </SessionContext.Provider>
  );
}

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useSession must be used inside an AuthProvider");
  return session;
}

/** Exported for the one place that needs to end a session imperatively. */
export { endSession };
