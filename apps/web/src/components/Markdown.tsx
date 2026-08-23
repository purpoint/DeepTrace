/**
 * Rendering the report, safely.
 *
 * This component is the only place in the application where text that came from
 * an arbitrary website is turned into markup, which makes it the one place a
 * cross-site scripting bug can live. Everything else renders untrusted values
 * as text, where React escapes them.
 *
 * Three defences, and each is needed for a different attack:
 *
 * *Raw HTML is never parsed.* react-markdown ignores embedded HTML unless
 * `rehype-raw` is added, and it is not added here. A page containing
 * `<script>` or an `onerror` attribute produces text, not an element.
 *
 * *The output is sanitized anyway.* `rehype-sanitize` runs over the tree with
 * GitHub's schema, so even if a future dependency starts emitting nodes this
 * file did not anticipate, the allowlist decides what survives. Defence in
 * depth, because the first line is a library's default and defaults change.
 *
 * *Link targets are checked here.* A markdown link is a URL from a page we do
 * not control, and `[click](javascript:...)` is a script execution that no HTML
 * sanitizer touches -- it is a legitimate anchor with a hostile scheme. Only
 * http and https survive.
 */

import type { MouseEvent } from "react";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";

const SAFE_SCHEMES = ["http:", "https:", "mailto:"];

/** Turn `[3]` in the prose into a link to citation 3.
 *
 *  Done as a text rewrite before parsing, rather than as a plugin over the
 *  tree, because markdown already knows how to make a link and an anchor is
 *  the whole feature: a reader clicks a number and lands on the passage.
 *
 *  Deliberately narrow. It matches a bracketed number and nothing else, so
 *  ordinary brackets in a quotation survive -- and because the replacement is
 *  itself markdown, a page that contains `[3](javascript:...)` cannot smuggle
 *  anything through: the URL written here is the only one used.
 */
export function linkCitations(markdown: string): string {
  return markdown.replace(/\[(\d{1,3})\]/g, (whole, number: string) => {
    // Inside the sources list the number is already a list marker, so a link
    // there would point a citation at itself.
    return `[${whole}](#citation-${number})`;
  });
}

/** Drop any URL whose scheme is not one we allow.
 *
 *  Returning an empty string rather than the original is deliberate: react
 *  renders an anchor with no href, which is inert and visibly not a link,
 *  instead of one that looks normal and runs code when clicked. */
export function safeUrl(url: string): string {
  try {
    const parsed = new URL(url, window.location.origin);
    return SAFE_SCHEMES.includes(parsed.protocol) ? url : "";
  } catch {
    // A relative path or something unparseable. Neither can carry a scheme,
    // so neither can execute.
    return url.startsWith("#") || url.startsWith("/") ? url : "";
  }
}

/** Scroll to a citation, and make the same link work twice.
 *
 *  A bare fragment link scrolls only on navigation, so clicking `[1]`, reading
 *  on, and clicking `[1]` again does nothing at all -- the hash is already
 *  what it would be set to, so the browser considers it a no-op. Found by
 *  clicking one twice.
 *
 *  Handling it here also lets the target be highlighted deliberately rather
 *  than relying on `:target`, which suffers from the same staleness.
 */
export function jumpToCitation(clicked: MouseEvent<HTMLAnchorElement>, href: string): void {
  const target = document.querySelector(href);
  if (!target) return; // a marker whose citation is not on this page

  clicked.preventDefault();
  target.scrollIntoView({ behavior: "smooth", block: "center" });

  // Re-triggered by hand so a second click flashes again. The class is removed
  // on the next frame so the browser sees it as a fresh addition.
  target.classList.remove("citation-hit");
  requestAnimationFrame(() => target.classList.add("citation-hit"));

  // The hash still gets set, so the link is copyable and the back button works.
  history.replaceState(null, "", href);
}

export function Markdown({
  children,
  linkCitationMarkers = false,
}: {
  children: string;
  linkCitationMarkers?: boolean;
}) {
  const source = linkCitationMarkers ? linkCitations(children) : children;

  return (
    <div className="prose-report">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSanitize]}
        urlTransform={safeUrl}
        components={{
          a: ({ href, children: text }) => {
            // A citation marker stays on the page; everything else leaves it.
            // They are different affordances and should not look alike.
            if (href?.startsWith("#citation-")) {
              return (
                <a
                  href={href}
                  onClick={(clicked) => jumpToCitation(clicked, href)}
                  // No horizontal margin: the prose already has a space before the
                  // marker, and a margin after it pushes the following comma
                  // away, rendering as "[1] , [2] ." instead of "[1], [2]."
                  className="rounded bg-brand/10 px-1 align-baseline font-mono text-[0.72em] text-brand no-underline transition-colors hover:bg-brand/25"
                >
                  {text}
                </a>
              );
            }

            return (
              <a
                href={href}
                // A link to a page chosen by an untrusted source: opening it in
                // a new tab without noopener would hand that page a reference
                // to this window, which it can use to navigate it elsewhere.
                target="_blank"
                rel="noopener noreferrer nofollow ugc"
                className="text-brand underline underline-offset-2 hover:brightness-125"
              >
                {text}
              </a>
            );
          },
          h1: ({ children: text }) => (
            <h1 className="mt-8 mb-3 text-2xl font-semibold tracking-tight text-ink">{text}</h1>
          ),
          h2: ({ children: text }) => (
            <h2 className="mt-10 mb-3 border-b border-line pb-2 text-base font-semibold tracking-tight text-ink">
              {text}
            </h2>
          ),
          p: ({ children: text }) => (
            <p className="my-3 leading-7 text-muted">{text}</p>
          ),
          ol: ({ children: text }) => (
            <ol className="my-3 list-decimal space-y-2 pl-6 text-muted marker:text-faint">{text}</ol>
          ),
          ul: ({ children: text }) => (
            <ul className="my-3 list-disc space-y-1 pl-6 text-muted marker:text-faint">{text}</ul>
          ),
          blockquote: ({ children: text }) => (
            <blockquote className="my-2 border-l-2 border-brand/40 pl-3 text-sm text-muted italic">
              {text}
            </blockquote>
          ),
          code: ({ children: text }) => (
            <code className="rounded bg-raised px-1 py-0.5 font-mono text-[0.85em] text-ink">{text}</code>
          ),
        }}
      >
        {source}
      </ReactMarkdown>
    </div>
  );
}
