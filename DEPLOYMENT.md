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

What CI does **not** do is bring the stack up and drive a request through TLS
end to end. That is the remaining gap in this milestone, and it is a gap in
verification rather than in the configuration.

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

### 3. Up

```bash
make up-deploy
```

Then `https://localhost:8443`. Port 8080 answers every request with a permanent
redirect and nothing else: a stack that serves both schemes is one where a
client that forgot the scheme keeps working, so nobody finds out the credential
travelled in clear.

---

## Why secrets come from files

An environment variable is not a private place. It is printed by `docker
inspect`, readable through `/proc/<pid>/environ` by anything running as the same
user, inherited by every child process the container ever spawns, and dumped
verbatim by a surprising number of crash handlers. None of that is a flaw in
this application; all of it is a way this application's keys leave it.

A file is read once, by the process that opens it, and Docker mounts it from a
read-only tmpfs that never touches the host disk.

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
