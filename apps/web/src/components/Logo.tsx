/**
 * The mark.
 *
 * A claim at the top, descending through two verification points to the source
 * it rests on -- which is the one thing this system does that a chatbot does
 * not. Drawn rather than lettered because the shape is the argument.
 *
 * Inline SVG, not a file: it takes its colour from the surrounding text, so it
 * is correct in both themes without a second asset to keep in sync.
 */

export function Logo({ className = "h-5 w-5" }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className} aria-hidden="true">
      {/* The trace: a path from the claim down to its source. */}
      <path
        d="M6 4.5 L6 12 Q6 15 9 15 L15 15 Q18 15 18 18 L18 19.5"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        className="text-brand"
      />
      <circle cx="6" cy="4.5" r="2.4" className="fill-brand" />
      <circle cx="12" cy="15" r="1.6" className="fill-brand/50" />
      <circle cx="18" cy="19.5" r="2.4" fill="none" strokeWidth="1.6" stroke="currentColor" className="text-brand" />
    </svg>
  );
}

export function Wordmark() {
  return (
    <span className="flex items-center gap-2">
      <Logo />
      <span className="text-sm font-semibold tracking-tight text-ink">
        Deep<span className="text-brand">Trace</span>
      </span>
    </span>
  );
}
