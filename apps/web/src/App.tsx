import { Link, Route, Routes, useLocation } from "react-router-dom";

import { useHealth } from "./api/hooks";
import { Wordmark } from "./components/Logo";
import { ThemeToggle } from "./theme";
import { Ask } from "./routes/Ask";
import { History } from "./routes/History";
import { Workspace } from "./routes/Workspace";

function Header() {
  const health = useHealth();
  const degraded = health.data && health.data.status !== "ok";
  const location = useLocation();

  return (
    <header className="sticky top-0 z-20 border-b border-line bg-canvas/85 backdrop-blur-md">
      <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-3">
        <Link to="/" className="transition-opacity hover:opacity-80">
          <Wordmark />
        </Link>

        <nav className="flex items-center gap-5 text-sm">
          <Link
            to="/"
            className={`transition-colors ${
              location.pathname === "/" ? "text-ink" : "text-muted hover:text-ink"
            }`}
          >
            Ask
          </Link>
          <Link
            to="/history"
            className={`transition-colors ${
              location.pathname === "/history" ? "text-ink" : "text-muted hover:text-ink"
            }`}
          >
            History
          </Link>

          {degraded ? (
            // Said plainly rather than left for the user to discover when
            // something fails: the API reports each dependency separately, so
            // the interface can name which one is missing.
            <span
              className="rounded-md bg-verdict-partial/10 px-2 py-0.5 text-xs text-verdict-partial ring-1 ring-inset ring-verdict-partial/25"
              title={`database ${health.data?.database ? "ok" : "down"}, queue ${
                health.data?.queue ? "ok" : "down"
              }`}
            >
              degraded
            </span>
          ) : null}

          <ThemeToggle />
        </nav>
      </div>
    </header>
  );
}

export function App() {
  return (
    <div className="min-h-screen">
      {/* A single soft wash of brand colour at the top of the page. Enough to
          make the product feel like it has a colour, faint enough that it never
          competes with a verdict badge for attention. */}
      <div
        aria-hidden
        className="pointer-events-none fixed inset-x-0 top-0 h-72 bg-gradient-to-b from-brand/[0.07] to-transparent"
      />
      <Header />
      <main className="relative">
        <Routes>
          <Route path="/" element={<Ask />} />
          <Route path="/history" element={<History />} />
          <Route path="/research/:researchId" element={<Workspace />} />
        </Routes>
      </main>
    </div>
  );
}
