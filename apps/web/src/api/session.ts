/**
 * The signed-in session: where the tokens live, and how they are renewed.
 *
 * Two credentials, stored two different ways, for a reason.
 *
 * The **access token** is held in a module variable and never written to disk.
 * It is the credential presented on every request, so it is the one an attacker
 * most wants; keeping it in memory means a reload throws it away, and nothing
 * on the machine has a copy to steal after the tab is closed.
 *
 * The **refresh token** is in localStorage, because something has to survive a
 * reload or signing in would last exactly as long as one page view. This is the
 * honest weak point: script running on this origin can read it. It is worth
 * writing down rather than implying otherwise -- the alternative that actually
 * closes it is an httpOnly cookie, which needs the API to set cookies and
 * brings CSRF along with it, and neither is free. What limits the damage today
 * is that untrusted text reaches the DOM in exactly one file, `Markdown.tsx`,
 * and it is sanitized there.
 *
 * **Refreshing is single-flight**, and that is not an optimisation. Refresh
 * tokens rotate, and presenting a spent one is how the server detects theft --
 * it destroys every session the account has. Three requests that expire at the
 * same moment and each start their own refresh would look exactly like that,
 * and would sign the user out for the crime of loading a page with three panels
 * on it. So the first one starts the refresh and the rest wait on the same
 * promise.
 */

const REFRESH_KEY = "deeptrace.refresh";

export interface Tokens {
  access_token: string;
  refresh_token: string;
  expires_in: number;
}

let accessToken: string | null = null;
let refreshing: Promise<string | null> | null = null;

/** Called when a session ends and cannot be renewed, so the UI can react. */
let onSignedOut: (() => void) | null = null;

export function whenSignedOut(handler: () => void): void {
  onSignedOut = handler;
}

export function accessTokenNow(): string | null {
  return accessToken;
}

export function hasStoredSession(): boolean {
  return storedRefreshToken() !== null;
}

export function storedRefreshToken(): string | null {
  try {
    return window.localStorage.getItem(REFRESH_KEY);
  } catch {
    // Storage can be unavailable: private browsing on some engines, or a
    // cookie policy that blocks it. Treated as "no stored session" rather than
    // as an error, so the app asks for a password instead of failing to load.
    return null;
  }
}

export function remember(tokens: Tokens): void {
  accessToken = tokens.access_token;
  try {
    window.localStorage.setItem(REFRESH_KEY, tokens.refresh_token);
  } catch {
    // The session still works for this tab; it just will not survive a reload.
  }
}

export function forget(): void {
  accessToken = null;
  try {
    window.localStorage.removeItem(REFRESH_KEY);
  } catch {
    /* nothing to clear */
  }
}

/**
 * Obtain a fresh access token, starting a refresh only if one is not running.
 *
 * Returns null when the session cannot be renewed, which is the signal to sign
 * in again -- the caller should not retry, because a refused refresh does not
 * become accepted by asking twice.
 */
export function refreshSession(): Promise<string | null> {
  if (refreshing) return refreshing;

  refreshing = (async () => {
    const token = storedRefreshToken();
    if (!token) return null;

    try {
      // A bare fetch, deliberately outside the client that retries on 401.
      // Routing this through it would mean a failed refresh triggers a
      // refresh, which is a loop with a network request in it.
      const response = await fetch("/api/auth/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: token }),
      });

      if (!response.ok) {
        endSession();
        return null;
      }

      const tokens = (await response.json()) as Tokens;
      remember(tokens);
      return tokens.access_token;
    } catch {
      // The network failed. The session is not known to be invalid, so the
      // stored token is kept -- discarding it here would sign someone out for
      // walking into a lift.
      return null;
    } finally {
      refreshing = null;
    }
  })();

  return refreshing;
}

/** Clear the session and tell the app. Called when renewal is refused. */
export function endSession(): void {
  forget();
  onSignedOut?.();
}
