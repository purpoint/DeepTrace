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

import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";

const SAFE_SCHEMES = ["http:", "https:", "mailto:"];

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

export function Markdown({ children }: { children: string }) {
  return (
    <div className="prose-report">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSanitize]}
        urlTransform={safeUrl}
        components={{
          a: ({ href, children: text }) => (
            <a
              href={href}
              // A link to a page chosen by an untrusted source: opening it in a
              // new tab without noopener would hand that page a reference to
              // this window, which it can use to navigate it somewhere else.
              target="_blank"
              rel="noopener noreferrer nofollow ugc"
              className="text-blue-700 underline underline-offset-2 hover:text-blue-900"
            >
              {text}
            </a>
          ),
          h1: ({ children: text }) => (
            <h1 className="mt-8 mb-3 text-2xl font-semibold text-slate-900">{text}</h1>
          ),
          h2: ({ children: text }) => (
            <h2 className="mt-8 mb-3 border-b border-slate-200 pb-1 text-lg font-semibold text-slate-800">
              {text}
            </h2>
          ),
          p: ({ children: text }) => (
            <p className="my-3 leading-7 text-slate-700">{text}</p>
          ),
          ol: ({ children: text }) => (
            <ol className="my-3 list-decimal space-y-2 pl-6 text-slate-700">{text}</ol>
          ),
          ul: ({ children: text }) => (
            <ul className="my-3 list-disc space-y-1 pl-6 text-slate-700">{text}</ul>
          ),
          blockquote: ({ children: text }) => (
            <blockquote className="my-2 border-l-2 border-slate-300 pl-3 text-sm text-slate-600 italic">
              {text}
            </blockquote>
          ),
          code: ({ children: text }) => (
            <code className="rounded bg-slate-100 px-1 py-0.5 font-mono text-sm">{text}</code>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
