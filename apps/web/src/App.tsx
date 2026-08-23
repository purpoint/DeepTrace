import { Link, Route, Routes } from "react-router-dom";

import { useHealth } from "./api/hooks";
import { Ask } from "./routes/Ask";
import { History } from "./routes/History";
import { Workspace } from "./routes/Workspace";

function Header() {
  const health = useHealth();
  const degraded = health.data && health.data.status !== "ok";

  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto flex max-w-4xl items-center justify-between px-6 py-3">
        <Link to="/" className="text-sm font-semibold tracking-tight text-slate-900">
          DeepTrace
        </Link>
        <nav className="flex items-center gap-4 text-sm text-slate-600">
          <Link to="/history" className="hover:text-slate-900">
            History
          </Link>
          {degraded ? (
            // Said plainly rather than left for the user to discover when
            // something fails: the API reports each dependency separately, so
            // the UI can name what is missing.
            <span
              className="rounded bg-amber-50 px-2 py-0.5 text-xs text-amber-800"
              title={`database ${health.data?.database ? "ok" : "down"}, queue ${
                health.data?.queue ? "ok" : "down"
              }`}
            >
              degraded
            </span>
          ) : null}
        </nav>
      </div>
    </header>
  );
}

export function App() {
  return (
    <div className="min-h-screen">
      <Header />
      <main>
        <Routes>
          <Route path="/" element={<Ask />} />
          <Route path="/history" element={<History />} />
          <Route path="/research/:researchId" element={<Workspace />} />
        </Routes>
      </main>
    </div>
  );
}
