/** Asking a question. The one screen a first-time visitor sees. */

import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { useHealth, useSubmit } from "../api/hooks";
import type { Depth } from "../api/types";
import { ApiError } from "../api/client";
import { Failure } from "../components/ui";

const DEPTHS: { value: Depth; label: string; detail: string }[] = [
  { value: "quick", label: "Quick", detail: "3 tasks, 8 sources — about 2 minutes" },
  { value: "standard", label: "Standard", detail: "6 tasks, 20 sources — about 5 minutes" },
  { value: "deep", label: "Deep", detail: "12 tasks, 50 sources — 15 minutes or more" },
];

export function Ask() {
  const [question, setQuestion] = useState("");
  const [depth, setDepth] = useState<Depth>("standard");
  const navigate = useNavigate();
  const submit = useSubmit();
  const health = useHealth();

  // The queue is what accepts work. Saying so before the user types a
  // paragraph is better than accepting it and failing on submit.
  const acceptingWork = health.data?.queue !== false;

  const onSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    submit.mutate(
      { question: question.trim(), depth },
      { onSuccess: (result) => navigate(`/research/${result.research_id}`) },
    );
  };

  const error = submit.error as ApiError | null;

  return (
    <div className="mx-auto max-w-2xl px-6 py-20">
      <div className="animate-fade-up">
        <h1 className="text-3xl font-semibold tracking-tight text-ink">
          What do you want to know?
        </h1>
        <p className="mt-3 text-sm leading-6 text-muted">
          Every answer cites the passage it came from, and every passage is checked
          against the page it was taken from. Nothing that fails the check is published.
        </p>
      </div>

      <form
        onSubmit={onSubmit}
        className="mt-10 space-y-5 animate-fade-up"
        style={{ animationDelay: "80ms" }}
      >
        <div>
          <textarea
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            rows={4}
            placeholder="Compare Kafka and RabbitMQ for high-scale microservices"
            className="w-full resize-y rounded-xl border border-line bg-surface px-4 py-3.5 text-ink shadow-sm transition-colors placeholder:text-faint focus:border-brand/60 focus:outline-none focus:ring-4 focus:ring-brand/10"
          />
          <div className="mt-2 flex justify-between text-xs text-faint">
            <span>A specific question produces better research than a topic.</span>
            <span>{question.trim().length}/2000</span>
          </div>
        </div>

        <fieldset className="grid gap-2 sm:grid-cols-3">
          {DEPTHS.map((option) => (
            <label
              key={option.value}
              className={`cursor-pointer rounded-xl border px-3.5 py-3 text-sm transition-all ${
                depth === option.value
                  ? "border-brand/60 bg-brand/10 text-ink shadow-sm"
                  : "border-line bg-surface text-muted hover:border-brand/30 hover:bg-raised"
              }`}
            >
              <input
                type="radio"
                name="depth"
                value={option.value}
                checked={depth === option.value}
                onChange={() => setDepth(option.value)}
                className="sr-only"
              />
              <span className="font-medium">{option.label}</span>
              <span className="mt-1 block text-xs text-faint">
                {option.detail}
              </span>
            </label>
          ))}
        </fieldset>

        {error ? (
          <Failure
            message={
              error.code === "unavailable"
                ? "The service cannot accept research right now. Try again shortly."
                : error.message
            }
            reference={error.reference}
          />
        ) : null}

        {!acceptingWork ? (
          <p className="rounded-xl bg-verdict-partial/10 px-3.5 py-2.5 text-sm text-verdict-partial ring-1 ring-inset ring-verdict-partial/25">
            The job queue is unreachable, so new research cannot be started. Finished
            research is still readable.
          </p>
        ) : null}

        <button
          type="submit"
          disabled={question.trim().length < 10 || submit.isPending || !acceptingWork}
          className="group inline-flex items-center gap-2 rounded-xl bg-brand px-5 py-2.5 text-sm font-medium text-canvas shadow-sm transition-all hover:brightness-110 hover:shadow-brand/20 disabled:cursor-not-allowed disabled:bg-raised disabled:text-faint disabled:shadow-none disabled:hover:brightness-100"
        >
          {submit.isPending ? "Starting…" : "Research this"}
          <span className="transition-transform group-hover:translate-x-0.5">→</span>
        </button>
      </form>
    </div>
  );
}
