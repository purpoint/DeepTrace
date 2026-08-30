# Deploying DeepTrace

Two ways to run the stack in containers. They differ in exactly two things, and
both are the difference between something that runs and something that can be
exposed.

| | `make up` | `make up-deploy` |
|---|---|---|
| Transport | HTTP on 8080 | **TLS on 8443**; 8080 redirects and serves nothing else |
| Secrets | environment variables | **files** under `/run/secrets` |
| Forwarded headers | not trusted | trusted from the compose subnet only |
| For | local, CI | anything reachable by someone else |

---

## What has actually been verified

This is the honest part, and it is first because it changes how much of the
rest to believe.

There is **no Docker daemon and no nginx on the development machine**, so
nothing below was run here. CI does that work instead, and until a run is green
the TLS configuration is asserted rather than demonstrated. What CI performs on
every push:

- both nginx configurations parsed by nginx itself, the TLS one against a real
  certificate, because `nginx -t` opens the certificate and key and a config
  that names them wrongly fails there rather than at deploy
- the deployment overlay resolved by `docker compose config`
- a secret written to a file, mounted, and read back out of `Settings` with its
  trailing newline gone

CI does not bring the stack up. `make verify-deploy` does, and it has: on
2026-08-29 the stack started, served over TLS, and was reached over the public
internet through a Cloudflare tunnel -- sign-in, a submitted run, a cited
report, and a WebSocket that connected and replayed the run's first event.

Running it the first time found **ten defects**, three latent since M24. The
container stack had never worked, and nothing said so because nothing had tried.

**The Render half of the split is deployed and verified**, on 2026-08-30, at
`deeptrace-api-ot29.onrender.com`: `/health` reporting database and queue
connected, an account registered through the open endpoint, a signed-in
research run that completed with 9 sources, 34 verified passages and 13 claims,
and a report carrying 30 citations across 17 inline markers.

Two things that answers questions this file could only guess at before. **The
worker survives beside the API in 512 MB** -- the compromise in
`scripts/serve-with-worker.sh` holds under a real run, which is the thing most
likely not to. And the grouped-citation fix from `0a7eac8` is visible in the
output: markers like `[1, 2, 3, 4, 5]` are parsed and validated, where before
they would have passed through unchecked with the report still calling itself
fully cited.

Getting there took five failed deploys. In order: a `COPY` of a path
`.dockerignore` excluded; then three separate resources still suspended from an
earlier attempt, each surfacing only once the one before it was fixed. Only the
first was a defect in this repository. The rest were the platform's state, and
the lesson is that **the error message named the cause in none of the five
cases** -- an eleven-second "exited with status 1", and a `socket.gaierror` for
a database that was merely asleep.

**The Vercel half has not been deployed.** `apps/web/vercel.json` and
`VITE_API_ORIGIN` are written and type-checked and nothing has run them, which
is the state the Render half was in before it produced five failures.

---

## Deploying

### 1. Secrets

```bash
make secrets
```

Writes five files into `deploy/secrets/`, which is git-ignored:

| File | Contents |
|---|---|
| `jwt_secret` | 48 random URL-safe bytes |
| `postgres_password` | 24 random URL-safe bytes |
| `database_url` | the connection string, **built from the password file** |
| `google_api_key` | empty — put your key in it |
| `tavily_api_key` | empty — put your key in it |

`database_url` is generated rather than written by hand because it contains the
same password as `postgres_password`, and a connection string cannot be
assembled from a file by compose interpolation. Two files that must agree, kept
in step by one command; edit either by hand and the next `up` fails to connect
for a reason that looks like a network problem.

The target refuses to overwrite an existing set. Regenerating rotates every
credential at once, which is rarely what anyone meant.

### 2. A certificate

For a real deployment, put a CA-issued certificate and its key at
`deploy/certs/fullchain.pem` and `deploy/certs/privkey.pem`.

To prove the stack terminates TLS at all:

```bash
make tls-cert           # or: TLS_HOST=research.example.com make tls-cert
```

Self-signed, and a browser will refuse it — correctly. It is for checking that
nginx serves 8443 and that 8080 redirects, not for serving anyone.

### 3. Up, and proved

```bash
make verify-deploy
```

This is the one command worth running first. It brings the stack up, waits for
the API's healthcheck (which already waits on PostgreSQL, Redis and a completed
migration, so waiting on it waits on all of them), then checks the things that
can only be checked by running:

- 8443 answers over TLS, and 8080 answers 301 and nothing else
- HSTS is present over TLS
- a served asset carries the content-security policy — the header that
  `add_header` inheritance had been silently dropping
- an account can be created, sign in works *through nginx*, and a research
  question is accepted with 202

It leaves the stack running and prints where to sign in. `make up-deploy` on its
own does the same thing without the checking.

Then open `https://localhost:8443`. Port 8080 answers every request with a
permanent redirect and nothing else: a stack that serves both schemes is one
where a client that forgot the scheme keeps working, so nobody finds out the
credential travelled in clear.

### 4. An account

The browser client requires one, and a freshly migrated database has none. The
password is prompted rather than passed as an argument — a password on a command
line is written to shell history and visible in `ps` to every other user on the
machine:

```bash
docker compose -f docker-compose.yml -f docker-compose.deploy.yml exec api \
    python -m core.cli users create you@example.com
```

`make verify-deploy` does this for you and writes the credentials to
`deploy/secrets/demo_account`.

Registration through the sign-in screen also works and is open to anyone who can
reach the service. On a public deployment that is worth thinking about before
you share the URL — see below.

---

## Putting it on the internet with a Cloudflare Tunnel

The stack runs on your own machine and a tunnel gives it a real hostname with a
real certificate. No cloud account, no card, no open inbound port on your router
— `cloudflared` makes an outbound connection and traffic comes back down it.

```bash
brew install cloudflared
make verify-deploy                    # the stack must be up first
cloudflared tunnel --url https://localhost:8443 --no-tls-verify
```

It prints a `https://<random>.trycloudflare.com` URL. That is the app.

**Point it at 8443, not 8080.** Under the deployment overlay, 8080 only
redirects, so a tunnel aimed there sends every visitor to `https://localhost`,
which is their own machine. `--no-tls-verify` is because the origin certificate
is self-signed: Cloudflare is not being asked to vouch for it, only to reach it.
The public certificate is Cloudflare's and is real.

WebSockets work through a tunnel, which matters here — live progress is the
whole point of the run screen.

### What this costs in honesty

**TLS is terminated twice**, and the outer one is Cloudflare's. The nginx
configuration in this repository still does real work — it is the origin's TLS,
and the redirect, headers and proxy rules all still apply — but the certificate
a visitor's browser validates is not yours. Worth saying plainly rather than
implying end-to-end control you do not have.

**It is up only while your machine is.** A quick tunnel's URL also changes every
time you restart it. A named tunnel with your own domain fixes the URL and needs
a free Cloudflare account.

**Registration is open.** Anyone with the link can create an account, and there
is no switch to turn that off. Combined with the quota below, one stranger can
consume the day's capacity.

### The quota is the real constraint

The Gemini free tier allows **20 model requests per day**, and a single research
run spends about seven of them — three on the strong tier. That is roughly **six
runs per day for the whole deployment**, shared across everyone who visits, and
resetting at midnight Pacific rather than UTC.

So a public instance of this is a demonstration, not a service. Share the link
with someone who will try one question, not somewhere it will be found. The
`submit` rate limit (20 per hour per user) does not help here, because the
binding limit is the provider's and it is counted across all users at once.

---

## Splitting it: the client on Vercel, the API on Render

The standard shape for a small product, and the one this repository now
supports. It is **not** the better architecture here, and that is worth saying
before the instructions rather than after them.

`docker-compose.yml` already serves the client: nginx hands out the built
assets and proxies `/api` beside them, on one origin. Splitting gives that up
and buys a nicer domain. What it costs:

- **CORS becomes load-bearing.** `CORS_ORIGINS` is empty by default and has
  never mattered. Now the product does not work without it.
- **The WebSocket must address the API directly.** A static host can proxy REST
  server-side; it cannot proxy an upgrade. So the progress stream is
  cross-origin, and `VITE_API_ORIGIN` exists for exactly this.
- **Two deployments have to move together.** A backend URL change is a frontend
  rebuild.

### What Render's free tier does to the design

`render.yaml` is `docker-compose.yml` folded to fit one free web service, and
it gives up three things:

| Compose | Render free | Why |
|---|---|---|
| Worker is its own service | Runs **beside the API** in one container | A Background Worker is a paid service type |
| Migrations are a one-shot job | Run at container start | No job type; and one instance cannot race itself |
| Runs whenever it is up | **Sleeps when idle** | Free web services do |

The sleeping one has a consequence worth understanding. A run in flight when
the service sleeps stops mid-way. It is not lost -- the queue is at-least-once,
the reservation expires, and the job is reclaimed and resumed from its
checkpoint -- but **nothing wakes the service on the queue's behalf**, so it
waits for a visitor. The durability the project built is what makes this
survivable rather than data loss.

A free database expires **thirty days after it is created** -- not the several
months this file first claimed. The instance provisioned on 2026-08-30 is dated
to be deleted on **2026-09-28**, and deleted is the word Render uses: not
paused, not archived. The demo stops working on a date nobody remembers, in a
way that will read as a bug.

`scripts/serve-with-worker.sh` exits if *either* process stops, so the platform
restarts the container. A worker that has quietly died inside a healthy-looking
web service is the specific failure that arrangement risks.

### Deploying it

**1. The API, on Render.** Point a Blueprint at `render.yaml`. It provisions the
web service, a PostgreSQL instance and a Key Value instance, wires
`DATABASE_URL` and `REDIS_URL`, and generates `JWT_SECRET`. Three values are
`sync: false` and must be entered by hand:

- `GOOGLE_API_KEY`
- `TAVILY_API_KEY`
- `CORS_ORIGINS` — the static site's URL, exactly, with the scheme and no
  trailing slash. Leave it until step 2 gives you one.

**2. The client, on Vercel.** Import the repository with **Root Directory set to
`apps/web`**; `apps/web/vercel.json` supplies the rest. Set one environment
variable:

```
VITE_API_ORIGIN=https://<your-service>.onrender.com
```

It is the whole base, not a host to prepend. nginx strips the `/api` prefix
before the API sees it, and there is no nginx here -- so the client drops the
prefix when this is set, and `/api/auth/login` would 404 on every call if it
did not.

**3. Close the loop.** Put the Vercel URL into `CORS_ORIGINS` on Render and
redeploy. Then create an account:

```bash
render ssh deeptrace-api -- python -m core.cli users create you@example.com
```

Or register through the sign-in screen, which is open.

### The content-security policy names the API, and had to wait for it

`apps/web/vercel.json` shipped without a CSP at first, deliberately: the policy
nginx serves has `connect-src 'self'`, and copying it here -- the obvious move --
would **block every API call**, because the API is no longer `'self'`. A guessed
backend host would have been worse than none.

So it was added once the host existed, and it is the one directive that differs
from the containerised policy:

```
connect-src 'self' https://deeptrace-api-ot29.onrender.com wss://deeptrace-api-ot29.onrender.com
```

**Both schemes, and this is the part worth checking.** `https:` covers the REST
calls; `wss:` covers the progress stream. Allowing only the first produces a
deployment where everything works except live progress -- which reads as a
broken feature rather than as a policy, and is the failure a reader of this file
is most likely to ship. Three tests hold it: that both schemes are present, that
they name the same host, and that every other directive still matches what nginx
serves, because a split deployment is a reason to widen one directive rather
than all of them.

Changing the API's URL means changing this line. They are two deployments that
now have to move together, which is the cost the top of this section warned
about.

---

## Why secrets come from files

An environment variable is not a private place. It is printed by `docker
inspect`, readable through `/proc/<pid>/environ` by anything running as the same
user, inherited by every child process the container ever spawns, and dumped
verbatim by a surprising number of crash handlers. None of that is a flaw in
this application; all of it is a way this application's keys leave it.

A file is read once, by the process that opens it, and by nothing else.

Be precise about what Compose does here, because the usual claim is wrong for
this setup: a `secrets:` entry with `file:` is **bind-mounted from the host**,
not materialised in a tmpfs. That is a Swarm behaviour. These secrets are
ordinary files on the deployment host, and their permissions are the ones that
matter.

Which is why `make secrets` writes them 0644 inside a 0700 directory, rather
than the 0600 that looks stricter. The application container runs as uid 10001;
a 0600 file owned by the deploying user is unreadable to it, and the container
refuses to start saying it cannot read a secret that is sitting right there.
**The directory is the boundary.** No other user on the host can traverse into
it, and the container reaches the file through its own mount without ever
walking that path.

Any setting can arrive this way — append `_FILE` and give a path:

```
JWT_SECRET_FILE=/run/secrets/jwt_secret
```

Three behaviours are deliberate, and each is a failure that would otherwise be
silent:

**A trailing newline is stripped.** Every secret manager and every `echo`
writes one. A signing key with a stray newline is simply a different key, and
the symptom — tokens minted before a redeploy no longer verifying — reads as a
session bug rather than a configuration one.

**Setting both forms is a startup error.** An operator migrating from variables
to files will have both set at some point. Quietly preferring one means they
believe they have rotated a credential that is still the old one.

**A named file that cannot be read is a startup error.** Falling back to "unset"
would start the application without the secret it was explicitly told where to
find, and then blame a variable the operator did set.

### The requirement lives in the application, not the compose file

`APP_ENV=production` refuses to start without `JWT_SECRET`, `DATABASE_URL`,
`GOOGLE_API_KEY` and `TAVILY_API_KEY`, and names every missing one in a single
message.

This used to be `${JWT_SECRET:?...}` in `docker-compose.yml`. It moved because
compose interpolates each file *before* merging an overlay, so a `:?` in the
base file is demanded even from a deployment that has deliberately replaced the
variable with a mounted file — the two mechanisms could not coexist. It is also
a weaker check: it guards one way of starting the application and only tests
that a string is non-empty, in the one place a test suite cannot see it.

As a property of `Settings` it holds for compose, for Kubernetes, for a systemd
unit and for a shell, and it is a unit test.

---

## Why the proxy is trusted, narrowly

The rate limiter counts against the socket's peer address. Behind nginx that
address *is* nginx, so every client on the internet would share one bucket.

The API is therefore started with:

```
--forwarded-allow-ips 172.28.0.0/16
```

uvicorn then rewrites the client address from `X-Forwarded-For`, but only for
connections arriving from the compose subnet — which is pinned in the overlay
precisely so it can be named here.

It is never `*`. Trusting the header unconditionally is worse than ignoring it:
a client that can choose its own forwarded address gets a fresh rate-limit
bucket on every request, and the limiter reports itself working while counting
nothing.

---

## TLS choices worth stating

**TLS 1.2 and 1.3 only.** 1.0 and 1.1 have no ciphers worth negotiating and are
refused by current browsers anyway.

**`ssl_prefer_server_ciphers off`.** With only AEAD suites on the list, the
client is the side that knows whether it has AES hardware acceleration, so it
should pick.

**Session tickets off.** nginx generates the ticket key at start and never
rotates it, so a ticket recovered later decrypts a session recorded earlier —
forward secrecy, given away to save a handshake.

**HSTS without `preload`.** One year, `includeSubDomains`. Preload is a
submission to a list compiled into browser binaries and getting off it takes
months; it is the right end state and the wrong thing to enable in the same
change that first serves a certificate.

**HSTS is emitted from a mapped variable** rather than written twice, so it is
the empty string over plain HTTP and nginx omits the header entirely. Browsers
ignore HSTS over HTTP regardless; the reason not to send it is that a header
present in a response nobody honours reads, to the next person, as protection
that is switched on.

---

## A note on nginx and `add_header`

`add_header` is inherited from an outer level **only if the current level
defines no `add_header` of its own**. This configuration had `location /assets/`
setting `Cache-Control`, which silently dropped four security headers from every
script and stylesheet the application serves — the exact responses a
content-security policy exists to constrain. The server block still listed them,
so nothing read as wrong.

The headers now live in `apps/web/snippets/security-headers.conf`, included at
the server level and again inside every location that sets a header of its own.
A test asserts that rule holds for every location in the shared snippet.

---

## Operating

```bash
make logs                                   # follow every service
make down-deploy                            # stop, keeping the volumes
docker compose -f docker-compose.yml -f docker-compose.deploy.yml up --scale worker=3 -d
```

Scaling workers is safe: the queue is at-least-once and every job carries its
research id, so a second worker resumes from the checkpoint rather than
repeating work that has already been paid for.

Certificate renewal is a file replacement plus `docker compose exec web nginx -s
reload`. Nothing else restarts.
