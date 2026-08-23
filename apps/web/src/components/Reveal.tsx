/**
 * Content that arrives as you reach it.
 *
 * Used on the report, where sections are long and a reader scrolls through them
 * one at a time. The effect is small on purpose: a twelve-pixel rise over half
 * a second reads as the page keeping up, while anything larger reads as the
 * page performing.
 *
 * An IntersectionObserver rather than a scroll listener, because a scroll
 * handler runs on every frame of every scroll and this needs to know one thing,
 * once, per element.
 *
 * Anything already on screen when the page loads is revealed immediately, with
 * no animation. Animating what the reader is already looking at is a flash for
 * no reason.
 */

import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

export function Reveal({
  children,
  delay = 0,
}: {
  children: ReactNode;
  delay?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [shown, setShown] = useState(false);

  useEffect(() => {
    const element = ref.current;
    if (!element) return;

    // Without observer support the content is simply visible. A progressive
    // enhancement that hides content when it fails is not an enhancement.
    if (typeof IntersectionObserver === "undefined") {
      setShown(true);
      return;
    }

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry?.isIntersecting) {
          setShown(true);
          observer.disconnect(); // once revealed, it stays revealed
        }
      },
      { rootMargin: "0px 0px -10% 0px", threshold: 0.05 },
    );

    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  return (
    <div
      ref={ref}
      style={shown ? { animationDelay: `${delay}ms` } : undefined}
      className={shown ? "animate-fade-up" : "opacity-0"}
    >
      {children}
    </div>
  );
}
