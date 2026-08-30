/**
 * Signing in, and creating an account.
 *
 * One screen with a mode rather than two routes. The two forms differ by a
 * single field's meaning -- is this password one you are proving or one you are
 * choosing -- and a person who arrives at the wrong one should be able to fix
 * that without losing what they typed.
 *
 * The password rule is shown before it is broken, not after. A minimum length
 * that only appears as a red message once the form is submitted is a rule the
 * interface knew and chose not to say.
 */

import { useState } from "react";

import { useSession } from "../auth";
import { ApiError } from "../api/client";
import { Logo } from "../components/Logo";
import { TraceChain } from "../components/TraceChain";
import { Failure } from "../components/ui";
import { ThemeToggle } from "../theme";

const MINIMUM_PASSWORD = 12;

export function SignIn() {
  const [mode, setMode] = useState<"sign-in" | "register">("sign-in");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  const session = useSession();
  const registering = mode === "register";

  // Only enforced client-side when choosing a password. Applying it at sign-in
  // would lock out anyone whose password predates the rule, and would leak the
  // rule's exact value to someone probing the form.
  const tooShort = registering && password.length > 0 && password.length < MINIMUM_PASSWORD;
  const ready = email.includes("@") && password.length > 0 && !tooShort && !busy;

  const onSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (registering) await session.register(email.trim(), password);
      else await session.signIn(email.trim(), password);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause : null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen">
      <div
        aria-hidden
        className="pointer-events-none fixed inset-x-0 top-0 h-72 bg-gradient-to-b from-brand/[0.07] to-transparent"
      />

      <div className="absolute right-6 top-5">
        <ThemeToggle />
      </div>

      {/* `min-h-screen` here, not only on the wrapper. Centring needs a height
          to centre within: the previous `justify-center` sat on an element
          sized by its own content, so it did nothing and the padding alone
          decided where the card landed -- against the top of the page. */}
      <div className="relative mx-auto flex min-h-screen max-w-5xl flex-col justify-center gap-14 px-6 py-16 lg:flex-row lg:items-center lg:gap-20">
        {/* What the visitor came to find out. Someone arriving at a bare
            sign-in form cannot tell this from any other chat interface, and
            cannot see anything at all without making an account first. */}
        <section className="lg:flex-1">
          <div className="animate-fade-up">
            <Logo className="h-7 w-7 lg:hidden" />
            <h2 className="mt-5 text-2xl font-semibold tracking-tight text-ink lg:mt-0 lg:text-3xl">
              Answers you can check.
            </h2>
            <p className="mt-3 max-w-md text-sm leading-6 text-muted">
              DeepTrace researches a question, then shows its work. Every sentence
              traces to a quotation, and every quotation is matched against the
              page it came from — so a citation that was never on the page is
              removed before you read it.
            </p>
          </div>

          <div className="mt-9 rounded-2xl border border-line bg-surface/60 p-6">
            <TraceChain />
          </div>
        </section>

        <section className="w-full lg:w-[22rem] lg:flex-none">
        <div className="animate-fade-up">
          <Logo className="hidden h-7 w-7 lg:block" />
          <h1 className="mt-5 text-2xl font-semibold tracking-tight text-ink lg:mt-4">
            {registering ? "Create an account" : "Sign in to DeepTrace"}
          </h1>
          <p className="mt-2 text-sm leading-6 text-muted">
            Research belongs to the account that asked for it. Nobody else can read
            it, including the questions.
          </p>
        </div>

        <form
          onSubmit={onSubmit}
          className="mt-8 space-y-4 animate-fade-up"
          style={{ animationDelay: "80ms" }}
        >
          <label className="block">
            <span className="text-xs font-medium text-muted">Email</span>
            <input
              type="email"
              value={email}
              autoComplete="username"
              onChange={(event) => setEmail(event.target.value)}
              className="mt-1.5 w-full rounded-xl border border-line bg-surface px-3.5 py-2.5 text-ink shadow-sm transition-colors placeholder:text-faint focus:border-brand/60 focus:outline-none focus:ring-4 focus:ring-brand/10"
            />
          </label>

          <label className="block">
            <span className="text-xs font-medium text-muted">Password</span>
            <input
              type="password"
              value={password}
              // Tells a password manager which of the two things is happening,
              // so it offers to save a new password rather than autofilling an
              // old one over it.
              autoComplete={registering ? "new-password" : "current-password"}
              onChange={(event) => setPassword(event.target.value)}
              className="mt-1.5 w-full rounded-xl border border-line bg-surface px-3.5 py-2.5 text-ink shadow-sm transition-colors focus:border-brand/60 focus:outline-none focus:ring-4 focus:ring-brand/10"
            />
            {registering ? (
              <span
                className={`mt-1.5 block text-xs ${tooShort ? "text-verdict-partial" : "text-faint"}`}
              >
                At least {MINIMUM_PASSWORD} characters. Length is the only rule — a
                long phrase beats a short one with symbols in it.
              </span>
            ) : null}
          </label>

          {error ? (
            <Failure
              message={
                error.code === "rate_limited"
                  ? `Too many attempts. ${error.message}`
                  : error.code === "conflict"
                    ? "An account already exists for that email. Sign in instead."
                    : error.code === "network" || error.code === "unavailable"
                      ? "Could not reach the service. It may be starting up."
                      : // A validation failure's envelope message is written for
                        // every endpoint at once and says nothing useful here.
                        // The field reason is the sentence a person can act on.
                        (error.fieldReason ?? error.message)
              }
              reference={error.reference}
            />
          ) : null}

          <button
            type="submit"
            disabled={!ready}
            className="group inline-flex w-full items-center justify-center gap-2 rounded-xl bg-brand px-5 py-2.5 text-sm font-medium text-canvas shadow-sm transition-all hover:brightness-110 hover:shadow-brand/20 disabled:cursor-not-allowed disabled:bg-raised disabled:text-faint disabled:shadow-none disabled:hover:brightness-100"
          >
            {busy
              ? registering
                ? "Creating…"
                : "Signing in…"
              : registering
                ? "Create account"
                : "Sign in"}
          </button>
        </form>

        <p className="mt-6 text-center text-sm text-muted animate-fade-up" style={{ animationDelay: "140ms" }}>
          {registering ? "Already have an account?" : "No account yet?"}{" "}
          <button
            onClick={() => {
              setMode(registering ? "sign-in" : "register");
              setError(null);
            }}
            className="font-medium text-brand transition-opacity hover:opacity-80"
          >
            {registering ? "Sign in" : "Create one"}
          </button>
        </p>
        </section>
      </div>
    </div>
  );
}
