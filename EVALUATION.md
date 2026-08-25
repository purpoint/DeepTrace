# Evaluation

Measured, not estimated. Every figure below came from a real run through
the ordinary pipeline; nothing here is projected, rounded up, or carried
over from an earlier configuration.

## What was measured

- **Measured at** — 2026-08-25T17:37:37+00:00
- **Commit** — `66c6c5d`
- **Cheap tier** — `gemini-3.5-flash-lite`
- **Strong tier** — `gemini-3.5-flash`
- **Depth budget** — quick
- **Questions attempted** — 24
- **Runs that produced a report** — 3

> 21 of 24 questions did not produce a report. Every
> average below is over the runs that did, and the count beside each
> figure says how many that was. A mean over the questions that happened
> to work is not the benchmark score unless it says so.

## Results

| Metric | Score | Runs measured | What it means |
|---|---|---|---|
| Citation correctness | 0.99 | 3/24 | Quotations that appear on the page they cite, re-verified from stored source text |
| Citation completeness | 0.39 | 3/24 | Asserting sections that carry at least one citation |
| Groundedness | 1.00 | 3/24 | Publishable claims that trace to evidence |
| Coverage | 0.67 | 3/24 | Declared concepts the specification's scope reached |
| Verbatim rate | 0.94 | 3/24 | Evidence matching its source word for word, rather than paraphrased |
| Source quality | 0.53 | 3/24 | Mean domain-based quality of the sources used |
| Publisher diversity | 0.84 | 3/24 | Distinct publishers per source (1.00 = every source a different site) |

**Question classification** — 3/3 questions were classified as the research type the benchmark expected.

**Contradictions surfaced** — 1/7 of the questions marked contested produced a reported disagreement rather than a single averaged position.

## Cost and latency

- **Total cost** — not measured for 22 of 24 runs, so no total
- **Total tokens** — 129,180
- **Mean latency** — 48.2s

## Per question

| Question | Report | Citations | Grounded | Coverage | Sources | Cost | Latency |
|---|---|---|---|---|---|---|---|
| `cmp-01` | yes | 1.00 | 1.00 | 0.50 | 9 | $0.0475 | 157s |
| `cmp-02` | yes | 0.96 | 1.00 | 1.00 | 9 | unknown | 194s |
| `cmp-03` | yes | 1.00 | 1.00 | 0.50 | 9 | $0.0456 | 167s |
| `cmp-04` | no | not measured | not measured | not measured | 0 | unknown | 95s |
| `cmp-05` | no | not measured | not measured | not measured | 0 | unknown | 25s |
| `exp-01` | no | not measured | not measured | not measured | 0 | unknown | 25s |
| `exp-02` | no | not measured | not measured | not measured | 0 | unknown | 25s |
| `exp-03` | no | not measured | not measured | not measured | 0 | unknown | 25s |
| `exp-04` | no | not measured | not measured | not measured | 0 | unknown | 24s |
| `exp-05` | no | not measured | not measured | not measured | 0 | unknown | 24s |
| `exp-06` | no | not measured | not measured | not measured | 0 | unknown | 25s |
| `inv-01` | no | not measured | not measured | not measured | 0 | unknown | 25s |
| `inv-02` | no | not measured | not measured | not measured | 0 | unknown | 25s |
| `inv-03` | no | not measured | not measured | not measured | 0 | unknown | 24s |
| `inv-04` | no | not measured | not measured | not measured | 0 | unknown | 24s |
| `rec-01` | no | not measured | not measured | not measured | 0 | unknown | 25s |
| `rec-02` | no | not measured | not measured | not measured | 0 | unknown | 25s |
| `rec-03` | no | not measured | not measured | not measured | 0 | unknown | 29s |
| `rec-04` | no | not measured | not measured | not measured | 0 | unknown | 25s |
| `rev-01` | no | not measured | not measured | not measured | 0 | unknown | 24s |
| `rev-02` | no | not measured | not measured | not measured | 0 | unknown | 25s |
| `rev-03` | no | not measured | not measured | not measured | 0 | unknown | 25s |
| `rev-04` | no | not measured | not measured | not measured | 0 | unknown | 70s |
| `rev-05` | no | not measured | not measured | not measured | 0 | unknown | 24s |

## Failures

- `cmp-04` — LLMRateLimitError: Gemini rate limit: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. 
* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.5-flash
Please retry in 51.621713026s. (google, gemini-3.5-flash)
- `cmp-05` — LLMRateLimitError: Gemini rate limit: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. 
* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.5-flash
Please retry in 26.568672048s. (google, gemini-3.5-flash)
- `exp-01` — LLMRateLimitError: Gemini rate limit: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. 
* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.5-flash
Please retry in 57.720550155s. (google, gemini-3.5-flash)
- `exp-02` — LLMRateLimitError: Gemini rate limit: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. 
* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.5-flash
Please retry in 21.128692223s. (google, gemini-3.5-flash)
- `exp-03` — LLMRateLimitError: Gemini rate limit: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. 
* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.5-flash
Please retry in 30.652407983s. (google, gemini-3.5-flash)
- `exp-04` — LLMRateLimitError: Gemini rate limit: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. 
* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.5-flash
Please retry in 16.748955046s. (google, gemini-3.5-flash)
- `exp-05` — LLMRateLimitError: Gemini rate limit: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. 
* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.5-flash
Please retry in 27.734282788s. (google, gemini-3.5-flash)
- `exp-06` — LLMRateLimitError: Gemini rate limit: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. 
* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.5-flash
Please retry in 32.592005141s. (google, gemini-3.5-flash)
- `inv-01` — LLMRateLimitError: Gemini rate limit: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. 
* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.5-flash
Please retry in 7.711028746s. (google, gemini-3.5-flash)
- `inv-02` — LLMRateLimitError: Gemini rate limit: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. 
* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.5-flash
Please retry in 42.439307476s. (google, gemini-3.5-flash)
- `inv-03` — LLMRateLimitError: Gemini rate limit: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. 
* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.5-flash
Please retry in 18.235019469s. (google, gemini-3.5-flash)
- `inv-04` — LLMRateLimitError: Gemini rate limit: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. 
* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.5-flash
Please retry in 54.015596934s. (google, gemini-3.5-flash)
- `rec-01` — LLMRateLimitError: Gemini rate limit: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. 
* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.5-flash
Please retry in 28.781646737s. (google, gemini-3.5-flash)
- `rec-02` — LLMRateLimitError: Gemini rate limit: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. 
* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.5-flash
Please retry in 4.265810758s. (google, gemini-3.5-flash)
- `rec-03` — LLMRateLimitError: Gemini rate limit: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. 
* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.5-flash
Please retry in 35.634920959s. (google, gemini-3.5-flash)
- `rec-04` — LLMRateLimitError: Gemini rate limit: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. 
* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.5-flash
Please retry in 10.97262434s. (google, gemini-3.5-flash)
- `rev-01` — LLMRateLimitError: Gemini rate limit: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. 
* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.5-flash
Please retry in 46.473403253s. (google, gemini-3.5-flash)
- `rev-02` — LLMRateLimitError: Gemini rate limit: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. 
* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.5-flash
Please retry in 21.713542724s. (google, gemini-3.5-flash)
- `rev-03` — LLMRateLimitError: Gemini rate limit: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. 
* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.5-flash
Please retry in 56.419218267s. (google, gemini-3.5-flash)
- `rev-04` — LLMRateLimitError: Gemini rate limit: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. 
* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.5-flash
Please retry in 46.296810471s. (google, gemini-3.5-flash)
- `rev-05` — LLMRateLimitError: Gemini rate limit: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. 
* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.5-flash
Please retry in 22.253516159s. (google, gemini-3.5-flash)

## What this does not measure

Nothing here scores whether an answer is correct, insightful, or useful.
Those need a human reader, and a number invented for them would be worse
than their absence -- it would be the exact failure this system exists to
prevent, committed by the thing meant to detect it.

No metric here asks a model to judge a model. Every figure is computed by
deterministic comparison against what the run stored, for the same reason
quotations are verified by string matching: a judge that shares the
generator's blind spots agrees with it, and the agreement scores well.
