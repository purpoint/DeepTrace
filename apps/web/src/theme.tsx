/**
 * Which theme, and who decides.
 *
 * Three sources of truth, in order: what the user chose here, what their
 * operating system says, and dark as the default. The user's choice wins
 * permanently, because a site that re-reads the OS on every visit is one whose
 * setting silently stops working the moment a laptop switches at dusk.
 *
 * The class is applied before React renders -- see the inline script in
 * index.html. Doing it in an effect means the first paint is the wrong theme,
 * and every visitor to a dark site sees a white flash on every load.
 */

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";

type Theme = "dark" | "light";

const STORAGE_KEY = "deeptrace.theme";

interface ThemeState {
  theme: Theme;
  toggle: () => void;
}

const Context = createContext<ThemeState>({ theme: "dark", toggle: () => {} });

export function resolveTheme(): Theme {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "dark" || stored === "light") return stored;
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  } catch {
    // Storage can throw in a private window or with cookies blocked. A theme
    // is not worth failing to render over.
    return "dark";
  }
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(() => resolveTheme());

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
  }, [theme]);

  const toggle = useCallback(() => {
    setTheme((current) => {
      const next = current === "dark" ? "light" : "dark";
      try {
        localStorage.setItem(STORAGE_KEY, next);
      } catch {
        // The toggle still works for this session; it just will not persist.
      }
      return next;
    });
  }, []);

  return <Context.Provider value={{ theme, toggle }}>{children}</Context.Provider>;
}

export const useTheme = () => useContext(Context);

export function ThemeToggle() {
  const { theme, toggle } = useTheme();
  const dark = theme === "dark";

  return (
    <button
      onClick={toggle}
      aria-label={dark ? "Switch to light theme" : "Switch to dark theme"}
      title={dark ? "Switch to light theme" : "Switch to dark theme"}
      className="flex h-6 w-11 items-center rounded-full border border-line bg-raised px-0.5 transition-colors hover:border-brand/50"
    >
      <span
        className={`flex h-4.5 w-4.5 items-center justify-center rounded-full bg-surface text-[9px] leading-none shadow-sm transition-transform duration-300 ${
          dark ? "translate-x-5" : "translate-x-0"
        }`}
        style={{ height: "1.125rem", width: "1.125rem" }}
      >
        {dark ? "🌙" : "☀"}
      </span>
    </button>
  );
}
