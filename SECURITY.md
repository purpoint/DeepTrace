# Security

DeepTrace fetches arbitrary web pages, feeds their contents to a language model,
and stores the result under a user's account. Each half of that sentence is a
threat surface, and they are different threats.

This describes what is defended, how, and — at the end — what is not.

---

## 1. The threat that is unusual here

Most applications treat user input as hostile. This one also treats **the
internet** as hostile, because a research agent's inputs are pages chosen by a
search engine, written by anyone.

A page can try to:

- **instruct the agent** — "ignore previous instructions and…"
- **fabricate support** — present text designed to be quoted as evidence
- **exfiltrate** — persuade the agent to fetch a URL carrying data
- **reach inward** — get the fetcher to request `169.254.169.254` or `localhost`
- **execute in a reader's browser** — script that survives into the report

Four of those five have a deterministic defence. The fifth does not, and is
named as such.

---

## 2. Prompt injection

### The corpus

Twelve attacks, each paired with **the layer expected to stop it**. An attack
with no named defence is one nobody has thought about; a defence with no attack
pointed at it is a claim.

| Defence | What it is |
|---|---|
| `sanitization` | Removed from the text before anything reads it |
| `fence` | Contained inside the delimited untrusted region |
| `preamble` | Left to the model, told to report rather than obey — the softest layer |
| `quote_verification` | Fabricated support rejected even under full compliance |
| `ssrf_guard` | The fetch an exfiltration needs is refused |
| `claim_grounding` | An assertion with nothing behind it is dropped |

**Seven of the twelve are stopped deterministically. Five are not**, and
`model_dependent()` names them — a corpus whose every case rests on "the model
behaved" measures the model's mood, not the system.

### The hole the corpus found

The untrusted-content fence used a **fixed delimiter**, so a page could simply
write the closing token itself. Everything after it then arrived where the model
expects task text rather than document text — defeating the preamble completely,
because the preamble governs what is *inside* the fence and the attacker has
stepped outside it.

The delimiter now carries a **per-call nonce**, so a page cannot close a fence
whose name it cannot predict, and delimiter-shaped text is stripped from the body
as well.

An existing test asserted that injections stay inside the fence, and it passed
throughout. Its attack was ordinary text that never tried to write a delimiter,
so the fence held trivially. **A containment test is worth only what its attack
attempts.**

Verified by reverting the fence and watching the corpus fail — which surfaced
something else: the nonce and the delimiter-stripping are redundant, and with the
nonce in place nothing detected the strip's removal. Untested code that a cleanup
would have deleted after checking the suite still passed. It has its own test now,
for the case the nonce does not cover.

---

## 3. Fetching

### SSRF

`fetch_url` accepts a URL chosen by a search engine or by a page. Every URL is
checked **before any request is made**:

- scheme must be `http` or `https`
- hostnames are **resolved**, and every resolved address is checked — not just
  the literal
- loopback, link-local, private and reserved ranges are refused, including
  `169.254.169.254`, the cloud metadata endpoint an exfiltration would aim at
- rejected, never sampled — a URL that fails is not tried on a different address

Resolving matters: `http://evil.test` that resolves to `127.0.0.1` is the attack,
and checking the string would miss it.

**Not closed: DNS rebinding.** The guard resolves, checks, and then the HTTP
client resolves again. A name that answers differently on the second lookup
defeats it. Closing this means pinning the checked address into the connection,
which is documented here rather than claimed as done.

### Sanitization

Applied at **both** ingestion points — fetched pages and search-provider
content — in the records themselves, so every provider inherits it. That is not
where it started: search results were unsanitized while fetched pages were
guarded, two entrances and one of them watched.

It removes executable blocks, hidden text and directional controls, and
**deliberately nothing else**. Verified lossless on 20,835 words of real
documentation, because a sanitizer that eats `List<String>` corrupts the evidence
this project exists to protect.

Found in passing: invisible characters were causing genuine evidence to verify as
`not_found` and be discarded — a real loss hidden behind a check that was working
correctly.

---

## 4. Rendering

Retrieved text reaches the DOM, so it is sanitized in **one file** on the way to
markup. Underneath that, the browser is told not to execute anything that slips
through:

```
default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';
img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'
```

`'unsafe-inline'` is allowed for **styles only** — the bundler inlines critical
CSS — and stated rather than quietly widened to cover scripts too.

**A control applied in three places out of four reviews as a control that
exists.** nginx inherits `add_header` from an outer level *only* if the current
level defines none of its own, and `location /assets/` sets `Cache-Control` — so
it silently dropped the CSP from every script and stylesheet served, the exact
responses the policy is written to constrain. The headers now live in a snippet
included at the server level and again in every location that sets one, with a
test that walks the locations.

The split deployment needs a wider `connect-src`, naming the API host over both
`https:` and `wss:`. Allowing only the first produces a site where everything
works except live progress — a broken feature rather than a visible policy error.

---

## 5. Accounts

| Control | Choice |
|---|---|
| Passwords | Argon2, minimum 12 characters, maximum 1024 |
| Tokens | 15-minute access (signature-verified), 14-day refresh (rotating, revocable) |
| WebSocket | single-use ticket, 30 seconds |
| Ownership | enforced in the query, not checked after |

The password maximum is a **denial-of-service control, not a policy**. Argon2 is
deliberately expensive, so an unbounded password field is a way to make the server
hash a megabyte on demand.

`JWT_SECRET` must be at least 32 characters, refused at startup. HS256 keys are
brute-forceable offline: an attacker with one token tries candidates as fast as
their hardware allows, with no server involved and nothing to rate limit.

**Ownership is in the query.** A filter applied afterwards is a filter a route can
forget. Two tests once passed with the filtering removed — a cost aggregate over
a run with no priced calls, and a history listing with nothing to exclude. *Tests
that fail before reaching their subject look like coverage.*

**Login does not disclose whether an address is registered**: same status, same
code, same sentence, and a dummy hash so the timing matches. Registration must
refuse a duplicate, so it does disclose — bounded by the rate limit rather than
denied, and stated in [API.md](API.md).

---

## 6. Secrets

**Never defaulted.** A placeholder key lets the application start broken and fail
forty seconds into a run. In production, four credentials are required at startup
and every missing one is named at once.

**Redacted from logs and traces in two layers**: sensitive field *names*, and
secret *value shapes* — `sk-`, `tvly-`, `ghp_`, URL credentials — caught wherever
they appear. The second layer is the one that matters, because leaks happen in
exception messages rather than in deliberate logging.

Redaction lives in one module and is applied where a trace record is constructed,
not in each of the five recorders. It did not start that way: `AgentRun.metadata`
was left unredacted while `ToolCall.metadata` was guarded — introduced by the same
commit that added the guard. **A half-applied control reviews as proof that the
control exists.**

**Supplied as files in a deployment.** `<NAME>_FILE` points at a path; an
environment variable is printed by `docker inspect`, readable through
`/proc/<pid>/environ`, inherited by every child process, and dumped by many crash
handlers. Setting both forms is a startup error rather than a silent preference —
an operator who has set both believes they rotated something they did not.

---

## 7. Bounded execution

Cost is a security property when the system can spend money on its own.

Every ceiling is arithmetic in code, not a prompt instruction, so no model can
reason past it: depth budgets, a round limit, a graph iteration limit, a
verification-loop budget, and two convergence checks.

---

## 8. What is not defended

Stated plainly, because a security document listing only controls is marketing.

| Open | Detail |
|---|---|
| **DNS rebinding** | The guard resolves and checks; the HTTP client resolves again. Closing it means pinning the address into the connection |
| **Refresh token in `localStorage`** | Script on this origin can read it. The access token is memory-only and untrusted text becomes markup in one sanitized file, but the honest close is an httpOnly cookie — which needs the API to set cookies and brings CSRF with it |
| **Access tokens cannot be revoked** | 15 minutes is the exposure window, and the price of verifying without a lookup |
| **Registration is open** | No switch to close it. On a public instance one visitor can consume the day's model quota |
| **Five injections are model-dependent** | Named by `model_dependent()` rather than counted as defended |
| **Keys were pasted in chat during development** | Both should be rotated |
| **The rate limiter trusts a configured proxy** | `--forwarded-allow-ips` names the hop. Set to `*` it would let a caller pick a fresh bucket per request |

## Reporting

This is a portfolio project with no production users. If you find something,
open an issue at [github.com/purpoint/DeepTrace](https://github.com/purpoint/DeepTrace).
