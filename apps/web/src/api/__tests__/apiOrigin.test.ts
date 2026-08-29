/**
 * Where the client sends its requests.
 *
 * The default -- this page's own origin -- is the arrangement worth
 * protecting: nginx serves the client and proxies /api beside it, so CORS is
 * not load-bearing and the WebSocket goes to the page's own host. These tests
 * exist because a split deployment gives that up, and the giving-up should be
 * deliberate and visible rather than a default that drifted.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

describe("the API origin", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("is the page's own origin unless configured otherwise", async () => {
    vi.resetModules();
    const { API_ORIGIN, eventsUrl } = await import("../client");

    expect(API_ORIGIN).toBe("");
    expect(eventsUrl("res_1", "tkt")).toContain(`//${window.location.host}/api/research/res_1/`);
  });

  it("drops the /api prefix when the API is addressed directly", async () => {
    // nginx and the dev server strip it; the API serves /auth/login, not
    // /api/auth/login. Prepending a host to "/api" would 404 on every call.
    vi.stubEnv("VITE_API_ORIGIN", "https://api.example.com");
    vi.resetModules();
    const { eventsUrl } = await import("../client");

    expect(eventsUrl("res_1", "tkt")).not.toContain("/api/");
  });

  it("addresses the API host directly for the socket, which is what a static host cannot proxy", async () => {
    vi.stubEnv("VITE_API_ORIGIN", "https://api.example.com");
    vi.resetModules();
    const { eventsUrl } = await import("../client");

    const url = eventsUrl("res_9", "tkt", 12);
    expect(url).toContain("wss://api.example.com/research/res_9/events");
    expect(url).toContain("after=12");
    expect(url).not.toContain(window.location.host);
  });

  it("keeps a plain-http origin on ws rather than forcing wss", async () => {
    vi.stubEnv("VITE_API_ORIGIN", "http://localhost:8000");
    vi.resetModules();
    const { eventsUrl } = await import("../client");

    expect(eventsUrl("res_1", "tkt")).toContain("ws://localhost:8000/research/res_1/");
  });
});
