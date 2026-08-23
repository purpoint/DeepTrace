import { useState } from "react";
import { Link, Route, Routes, useLocation } from "react-router-dom";

import { useHealth } from "./api/hooks";
import { useSession } from "./auth";
import { Wordmark } from "./components/Logo";
import { Spinner } from "./components/ui";
import { ThemeToggle } from "./theme";
import { Ask } from "./routes/Ask";
import { History } from "./routes/History";
import { SignIn } from "./routes/SignIn";
import { Workspace } from "./routes/Workspace";

/** The signed-in account, and the way out.
 *
 *  A menu rather than a bare "sign out" link, because sign-out is not an
 *  action anyone wants to be one careless click from -- and because the email
 *  is worth showing: on a shared machine, "signed in as who" is the question
 *  the header should answer without being asked. */
function AccountMenu() {
  const session = useSession();
  const [open, setOpen] = useState(false);

  if (!session.account) return null;
  const initial = session.account.email[0]?.toUpperCase() ?? "?";

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((shown) => !shown)}
        aria-haspopup="menu"
        aria-expanded={open}
        className="flex h-7 w-7 items-center justify-center rounded-full bg-brand/15 text-xs font-medium text-brand ring-1 ring-inset ring-brand/25 transition-colors hover:bg-brand/25"
      >
        {initial}
      </button>

      {open ? (
        <>
          {/* Catches the click that dismisses the menu. Without it the menu
              closes only when its own button is pressed again, which is not
              what anyone expects from a menu. */}
          <div className="fixed inset-0 z-30" onClick={() => setOpen(false)} />
          <div
            role="menu"
            className="absolute right-0 z-40 mt-2 w-60 rounded-xl border border-line bg-surface p-1.5 shadow-lg"
          >
            <p className="truncate px-2.5 py-2 text-xs text-muted" title={session.account.email}>
              {session.account.email}
            </p>
            <div className="my-1 h-px bg-line" />
            <button
              onClick={() => {
                setOpen(false);
                void session.signOut();
              }}
              className="w-full rounded-lg px-2.5 py-2 text-left text-sm text-ink transition-colors hover:bg-raised"
            >
              Sign out
            </button>
          </div>
        </>
      ) : null}
    </div>
  );
}

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
          <AccountMenu />
        </nav>
      </div>
    </header>
  );
}

export function App() {
  const session = useSession();

  // Three states, not two. On reload the access token is gone but the session
  // is not over, and a two-state guard would flash the sign-in screen at
  // someone who is still signed in -- they watch their session appear to end
  // and then un-end.
  if (session.status === "checking") {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner label="Restoring your session" />
      </div>
    );
  }

  if (session.status === "signed-out") return <SignIn />;

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
