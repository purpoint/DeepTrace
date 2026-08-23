/**
 * Tests for the one place untrusted text becomes markup.
 *
 * Every string these tests pass in is something a retrieved page could contain,
 * because the report is written from passages quoted out of pages this system
 * does not control. A bug here is not a rendering glitch -- it is script
 * execution in a user's session, and it would look completely normal.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Markdown, safeUrl } from "../Markdown";

describe("rendering untrusted markdown", () => {
  it("does not execute a script tag", () => {
    const { container } = render(
      <Markdown>{`Ordering holds <script>window.__owned = true</script> per partition.`}</Markdown>,
    );

    expect(container.querySelector("script")).toBeNull();
    expect((window as unknown as Record<string, unknown>).__owned).toBeUndefined();
  });

  it("does not create an element carrying an event handler", () => {
    const { container } = render(
      <Markdown>{`<img src="x" onerror="window.__owned = true" />`}</Markdown>,
    );

    expect(container.querySelector("img")).toBeNull();
    expect((window as unknown as Record<string, unknown>).__owned).toBeUndefined();
  });

  it("refuses a javascript: link", () => {
    // The attack a HTML sanitizer does not catch: a legitimate anchor whose
    // href is a script. It has to be stopped by inspecting the URL.
    //
    // Written without spaces or parentheses because markdown would not parse
    // those as a link at all -- and a payload that fails to parse tests
    // nothing. An attacker writes the one that works.
    render(<Markdown>{`[Read the docs](javascript:window.__owned=true)`}</Markdown>);

    const link = screen.getByText("Read the docs").closest("a");
    expect(link?.getAttribute("href")).toBeFalsy();
  });

  it("refuses a data: link", () => {
    render(
      <Markdown>
        {`[Download](data:text/html;base64,PHNjcmlwdD53aW5kb3cuX19vd25lZD0xPC9zY3JpcHQ+)`}
      </Markdown>,
    );

    expect(screen.getByText("Download").closest("a")?.getAttribute("href")).toBeFalsy();
  });

  it("keeps an ordinary link, and opens it without handing over the window", () => {
    render(<Markdown>{`[Kafka docs](https://kafka.apache.org/documentation)`}</Markdown>);

    const link = screen.getByText("Kafka docs").closest("a");
    expect(link?.getAttribute("href")).toBe("https://kafka.apache.org/documentation");
    // Without noopener, the opened page gets a reference to this window and can
    // navigate it somewhere else.
    expect(link?.getAttribute("rel")).toContain("noopener");
    expect(link?.getAttribute("rel")).toContain("noreferrer");
  });

  it("renders the report's actual shape", () => {
    render(
      <Markdown>{`## Findings\n\nOrder is preserved within a partition [1].\n\n- keyed records land together`}</Markdown>,
    );

    expect(screen.getByText("Findings")).toBeInTheDocument();
    expect(screen.getByText(/Order is preserved within a partition/)).toBeInTheDocument();
    expect(screen.getByText("keyed records land together")).toBeInTheDocument();
  });
});

describe("safeUrl", () => {
  it.each([
    ["https://example.com/page", "https://example.com/page"],
    ["http://example.com", "http://example.com"],
    ["mailto:someone@example.com", "mailto:someone@example.com"],
    ["#citation-3", "#citation-3"],
    ["/history", "/history"],
  ])("allows %s", (input, expected) => {
    expect(safeUrl(input)).toBe(expected);
  });

  it.each([
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "  javascript:alert(1)",
  ])("refuses %s", (input) => {
    expect(safeUrl(input)).toBe("");
  });
});
