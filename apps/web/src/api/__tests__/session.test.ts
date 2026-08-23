/**
 * Tests for the session module.
 *
 * One property here matters more than the rest. Refresh tokens rotate, and
 * presenting a spent one is how the server detects theft -- it destroys every
 * session the account has. A page that renders three panels, all of whose
 * requests expire at the same moment, would start three refreshes with the same
 * token, and two of them would look exactly like a thief. Single-flight
 * refreshing is what stands between that and users being signed out for opening
 * a page.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  accessTokenNow,
  endSession,
  forget,
  hasStoredSession,
  refreshSession,
  remember,
  storedRefreshToken,
  whenSignedOut,
} from "../session";

const tokens = (suffix: string) => ({
  access_token: `access-${suffix}`,
  refresh_token: `refresh-${suffix}`,
  expires_in: 900,
});

beforeEach(() => {
  window.localStorage.clear();
  forget();
  whenSignedOut(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("storing a session", () => {
  it("keeps the access token out of storage", () => {
    // It is the credential presented on every request, so it is the one worth
    // stealing. In memory it does not survive the tab being closed.
    //
    // Every key is read rather than the storage object stringified: a Storage
    // implementation does not have to expose its entries as own properties,
    // and stringifying one that does not would assert nothing at all.
    remember(tokens("1"));

    const stored = Array.from({ length: window.localStorage.length }, (_, index) =>
      window.localStorage.getItem(window.localStorage.key(index)!),
    );

    expect(accessTokenNow()).toBe("access-1");
    expect(stored).not.toContain("access-1");
    expect(stored).toContain("refresh-1");
  });

  it("keeps the refresh token so a reload is not a sign-out", () => {
    remember(tokens("1"));

    expect(storedRefreshToken()).toBe("refresh-1");
    expect(hasStoredSession()).toBe(true);
  });

  it("forgets both when the session ends", () => {
    remember(tokens("1"));

    forget();

    expect(accessTokenNow()).toBeNull();
    expect(hasStoredSession()).toBe(false);
  });

  it("survives storage being unavailable", () => {
    // Private browsing on some engines, or a cookie policy that blocks it. The
    // session should work for this tab rather than the app failing to load.
    //
    // Spied on the instance, not on Storage.prototype: the test environment's
    // storage need not inherit from it, and a spy on a prototype nothing uses
    // is a test that asserts the happy path while claiming otherwise.
    vi.spyOn(window.localStorage, "setItem").mockImplementation(() => {
      throw new Error("denied");
    });

    remember(tokens("1"));

    expect(accessTokenNow()).toBe("access-1");
    expect(hasStoredSession()).toBe(false);
  });
});

describe("refreshing", () => {
  it("exchanges the stored token for a new pair", async () => {
    remember(tokens("1"));
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => tokens("2") }),
    );

    const renewed = await refreshSession();

    expect(renewed).toBe("access-2");
    expect(storedRefreshToken()).toBe("refresh-2");
  });

  it("runs one refresh no matter how many callers ask at once", async () => {
    // The property this module exists for. Three refreshes with one token is
    // indistinguishable from a stolen token being replayed, and the server
    // responds by ending every session the account has.
    remember(tokens("1"));
    const fetcher = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => tokens("2") });
    vi.stubGlobal("fetch", fetcher);

    const results = await Promise.all([
      refreshSession(),
      refreshSession(),
      refreshSession(),
    ]);

    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(results).toEqual(["access-2", "access-2", "access-2"]);
  });

  it("allows a later refresh once the first has finished", async () => {
    // Single-flight must not mean single-ever: the promise is cleared when it
    // settles, or the session can never be renewed a second time.
    remember(tokens("1"));
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => tokens("2") }),
    );

    await refreshSession();
    const again = await refreshSession();

    expect(again).toBe("access-2");
  });

  it("ends the session when the server refuses", async () => {
    remember(tokens("1"));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 401 }));
    const signedOut = vi.fn();
    whenSignedOut(signedOut);

    const renewed = await refreshSession();

    expect(renewed).toBeNull();
    expect(hasStoredSession()).toBe(false);
    expect(signedOut).toHaveBeenCalled();
  });

  it("keeps the session when the network fails", async () => {
    // A refused refresh means the session is over. A network error means
    // nothing of the kind, and discarding the token here would sign someone
    // out for walking into a lift.
    remember(tokens("1"));
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));

    const renewed = await refreshSession();

    expect(renewed).toBeNull();
    expect(storedRefreshToken()).toBe("refresh-1");
  });

  it("does not call the server when there is nothing to refresh", async () => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);

    const renewed = await refreshSession();

    expect(renewed).toBeNull();
    expect(fetcher).not.toHaveBeenCalled();
  });
});

describe("ending a session", () => {
  it("clears storage and notifies once", () => {
    remember(tokens("1"));
    const signedOut = vi.fn();
    whenSignedOut(signedOut);

    endSession();

    expect(accessTokenNow()).toBeNull();
    expect(hasStoredSession()).toBe(false);
    expect(signedOut).toHaveBeenCalledTimes(1);
  });
});
