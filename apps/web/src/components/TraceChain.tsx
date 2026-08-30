/**
 * The chain a sentence hangs from, shown rather than described.
 *
 * This is the argument for the whole project in one graphic: a sentence in a
 * report, the claim behind it, the passage that supports it, and the page that
 * passage was taken from -- each a real row in the database and a real screen
 * in this client.
 *
 * It sits beside the sign-in form because a visitor who has not made an account
 * can otherwise see nothing at all, and "another chat interface" is what they
 * will reasonably assume. The example is fixed rather than fetched: a landing
 * page that waits on an API is a landing page that is blank while a free
 * instance wakes up.
 */

const STEPS = [
  {
    label: "REPORT",
    body: (
      <p className="text-ink">
        “Kafka preserves record order within a single partition{" "}
        <span className="font-mono text-xs text-brand">[1]</span>.”
      </p>
    ),
  },
  {
    label: "CLAIM",
    body: (
      <p className="text-muted">
        <span className="font-medium text-verdict-supported">supported</span>
        <span className="text-faint"> · </span>2 publishers
        <span className="text-faint"> · </span>strength 1.00
      </p>
    ),
  },
  {
    label: "EVIDENCE",
    body: (
      <div className="text-muted">
        <p>
          “Records sent by a producer to a particular partition are appended in
          the order they are sent.”
        </p>
        <p className="mt-1.5 font-mono text-xs text-verdict-supported">
          verbatim ✓ checked against the page
        </p>
      </div>
    ),
  },
  {
    label: "SOURCE",
    body: (
      <p className="text-muted">
        kafka.apache.org
        <span className="text-faint"> · </span>official docs
        <span className="text-faint"> · </span>quality 0.97
      </p>
    ),
  },
];

export function TraceChain() {
  return (
    <ol className="space-y-7">
      {STEPS.map((step, index) => (
        <li
          key={step.label}
          className="relative pl-8 animate-fade-up"
          style={{ animationDelay: `${120 + index * 90}ms` }}
        >
          {/* A segment per step rather than one rail down the whole list. A
              single rail is anchored to the container, so it runs past the last
              dot by however tall that step's text happens to be -- a line
              trailing off into nothing under the final link of a chain. Each
              segment instead starts at its own dot and reaches into the gap
              below, which ends the line exactly where the chain does.

              The reach is the 1.75rem gap from `space-y-7` plus the 0.375rem
              the next dot sits below its item's top. Without that second term
              the line stops short and the chain reads as four separate rows --
              which is precisely the opposite of the point. */}
          {index < STEPS.length - 1 && (
            <span
              aria-hidden
              className="absolute -bottom-[2.125rem] left-[5px] top-4 w-px bg-line"
            />
          )}

          <span
            aria-hidden
            className={`absolute left-0 top-1.5 h-[11px] w-[11px] rounded-full border-2 ${
              index === 0 ? "border-brand bg-brand/30" : "border-line bg-canvas"
            }`}
          />
          <p className="font-mono text-[10px] font-medium tracking-[0.14em] text-faint">
            {step.label}
          </p>
          <div className="mt-1.5 text-sm leading-6">{step.body}</div>
        </li>
      ))}
    </ol>
  );
}
