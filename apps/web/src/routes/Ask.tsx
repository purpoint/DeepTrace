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
    <div className="mx-auto max-w-2xl px-6 py-16">
      <h1 className="text-2xl font-semibold text-slate-900">What do you want to know?</h1>
      <p className="mt-2 text-sm text-slate-600">
        Every answer cites the passage it came from, and every passage is checked against
        the page it was taken from.
      </p>

      <form onSubmit={onSubmit} className="mt-8 space-y-5">
        <div>
          <textarea
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            rows={4}
            placeholder="Compare Kafka and RabbitMQ for high-scale microservices"
            className="w-full resize-y rounded-lg border border-slate-300 px-4 py-3 text-slate-900 placeholder:text-slate-400 focus:border-slate-500 focus:outline-none"
          />
          <div className="mt-1 flex justify-between text-xs text-slate-500">
            <span>A specific question produces better research than a topic.</span>
            <span>{question.trim().length}/2000</span>
          </div>
        </div>

        <fieldset className="grid gap-2 sm:grid-cols-3">
          {DEPTHS.map((option) => (
            <label
              key={option.value}
              className={`cursor-pointer rounded-lg border px-3 py-2 text-sm ${
                depth === option.value
                  ? "border-slate-900 bg-slate-900 text-white"
                  : "border-slate-200 bg-white hover:border-slate-400"
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
              <span
                className={`mt-0.5 block text-xs ${
                  depth === option.value ? "text-slate-300" : "text-slate-500"
                }`}
              >
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
          <p className="rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-800">
            The job queue is unreachable, so new research cannot be started. Finished
            research is still readable.
          </p>
        ) : null}

        <button
          type="submit"
          disabled={question.trim().length < 10 || submit.isPending || !acceptingWork}
          className="rounded-lg bg-slate-900 px-5 py-2.5 text-sm font-medium text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          {submit.isPending ? "Starting…" : "Research this"}
        </button>
      </form>
    </div>
  );
}
