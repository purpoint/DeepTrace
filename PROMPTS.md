# Prompts

Eight prompts, in `core/prompts/`. Each is registered by name and version, and
every model call records which version produced its output — so a change in
behaviour can be attributed to a change in wording rather than guessed at.

Nothing here is a string built at a call site. A prompt assembled inline is a
prompt nobody can version, diff, or attribute a regression to.

---

## The registry

```python
registry.get("reporter", "v1")     # → Prompt
registry.ids()                     # → every registered id
registry.versions("reporter")      # → every version of one
```

A `Prompt` carries its `system` text, a `user_template` with `$variables`, the
**set of variables it requires**, its `tier`, and a description of what it is for.

Rendering with a missing variable raises rather than substituting an empty
string. A prompt silently missing its evidence still returns fluent text, and
fluent text with nothing behind it is precisely what this system exists to
prevent.

## The eight

| Prompt | Tier | Requires |
|---|---|---|
| `query_analyzer.v1` | cheap | `question`, `depth` |
| `planner.v1` | **strong** | `question`, `research_type`, `scope`, `assumptions`, `constraints`, `out_of_scope`, `depth`, `max_tasks` |
| `query_generator.v1` | cheap | `question`, `objective`, `query_count`, `freshness`, `refinement`, `source_requirements` |
| `sufficiency_check.v1` | cheap | `question`, `material`, `source_count`, `rounds` |
| `evidence_extractor.v1` | cheap | `question`, `document`, `source_title`, `source_domain`, `max_items` |
| `analyst.v1` | **strong** | `question`, `research_type`, `scope`, `evidence`, `evidence_count`, `gaps` |
| `fact_checker.v1` | cheap | `claim`, `condition`, `question`, `cited`, `related` |
| `reporter.v1` | **strong** | `question`, `interpretation`, `claims`, `gaps` |

Three strong-tier prompts — planner, analyst, reporter — and that is the whole
cost story: a run spends about seven model calls, three of them here. When the
free tier allows twenty requests a day, those three are the budget.

---

## Two rules that shape all of them

### A prompt is given what it may use, and nothing else

The reporter receives **claims only**. Not sources, not search results, not the
analyst's reasoning. It *cannot* cite a page the fact checker rejected, because
it is never shown one — which is a stronger guarantee than instructing it not to.

The same shape recurs: the fact checker is shown a claim's cited evidence *and*
related evidence it did not cite, because a check that only sees supporting
material can only agree.

### Retrieved text is fenced, never concatenated

Every page, document and search result reaches a model inside a delimited region
with a preamble saying it is data to be reported on, not instruction to follow.

**The delimiter carries a per-call nonce.** A fixed marker is one the content can
write for itself: a page containing the closing token ends the quoted region
early, and everything after it arrives where the model expects *task* text. That
defeats the preamble completely, because the preamble governs what is inside the
fence and the attacker has stepped outside it.

Text that merely *looks* like a delimiter is stripped from the body too, so
near-misses cannot muddy the transcript.

This was a real hole, found by the injection corpus written to test it. See
[SECURITY.md](SECURITY.md).

---

## What each one is for

**`query_analyzer`** — turns a question into a specification: research type,
scope, success criteria, time sensitivity, and the ambiguities it had to resolve.
Cheap tier: classification, not reasoning.

**`planner`** — decomposes the specification into atomic, independently
researchable tasks. Strong tier, because a bad decomposition wastes every call
downstream of it and no later stage can recover from it.

**`query_generator`** — turns one task into search queries. Runs per task, and
again per refinement round, so it is the highest-volume prompt in a run.

**`sufficiency_check`** — asks whether a task has enough material to stop
searching. The alternative is a fixed number of rounds, which either wastes calls
on easy tasks or starves hard ones.

**`evidence_extractor`** — pulls supporting passages from one page. It is
instructed to quote, not summarise, because **a paraphrase cannot be verified
against the source** — and every passage it returns is then checked by string
matching regardless of what it claims.

**`analyst`** — findings, trade-offs, contradictions and open questions, from
verified evidence. Told that a conclusion citing nothing is worse than a missing
conclusion, and every finding must carry the evidence ids behind it.

**`fact_checker`** — one claim at a time, against evidence including passages the
claim did not cite. Returns a verdict, and where a claim reaches past its
support, the narrower statement the evidence *does* carry.

**`reporter`** — the six written sections. Strong tier because it is the only
output a person reads directly, and a verified claim restated one degree too
strongly undoes the quote verifier, the grounding pass and the fact checker in a
sentence.

---

## Versioning

Every prompt is `v1`. That is honest rather than impressive: none has needed a
second version, and inventing one to look rigorous would defeat the purpose.

The machinery is in place for when one does. Registering `reporter.v2` leaves
`v1` retrievable, every recorded run keeps the version that produced it, and a
metric that moves can be attributed to the change.

**A change that is not a new version is a change nobody can attribute.** The
citation instruction added to the reporter's schema — see below — was such a
change, and the benchmark it should be measured against has not been re-run.

---

## The lesson that cost the most

`REPORTER_SYSTEM` asks for bracketed citations in a paragraph of its own. The
model followed it erratically: across three benchmark runs the report's prose
carried inline citations in 0 of 4, 1 of 6, and 5 of 5 sections.

The cause was not the model and not the prompt. `DraftSection.body` reached the
provider as `{"maxLength": 6000, "type": "string"}` — **no description, nothing
said** — while its own sibling `claim_ids` carried one.

> **A schema field with nothing to say is a field the model fills however it
> likes.**

With structured output, the schema *is* part of the prompt. Instructions eight
hundred tokens earlier compete with a decoder busy satisfying a shape. The field
now states the requirement where the model is answering it, with an example.

Pinned by a test that reads the schema **as the provider receives it** — asserting
on the Pydantic model would pass while `to_gemini_schema` dropped the description
on the way out.
