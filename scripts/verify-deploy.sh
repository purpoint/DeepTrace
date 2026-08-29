#!/usr/bin/env bash
#
# Bring the deployment stack up and prove it works, end to end, over TLS.
#
# This exists because everything in M26 was written on a machine with no Docker
# daemon. CI parses both nginx configurations and reads a secret back out of
# Settings, but nothing has ever started the stack or driven a request through
# it. That is the gap this closes, and it is the only thing that can.
#
#   make verify-deploy
#
# Leaves the stack running on success so you can look at it. `make down-deploy`
# stops it.

set -euo pipefail

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.deploy.yml"
BASE="https://localhost:${HTTPS_PORT:-8443}"
# example.com is reserved by RFC 2606 for exactly this. `verify@localhost` was
# the obvious choice and is not a valid address: the CLI created the account and
# the API refused every sign-in with it, 422.
EMAIL="${DEMO_EMAIL:-verify@example.com}"
ACCOUNT_FILE="deploy/secrets/demo_account"

# -k throughout: the certificate from `make tls-cert` is self-signed, and a
# browser refusing it is the correct behaviour. This checks that the stack
# terminates TLS, not that a CA vouched for it.
CURL="curl --silent --show-error --insecure --max-time 30"

step()  { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
fail()  { printf '\033[31mFAIL\033[0m  %s\n' "$1" >&2; exit 1; }
ok()    { printf '\033[32mok\033[0m    %s\n' "$1"; }

# ---------------------------------------------------------------------------
step "Preconditions"

command -v docker >/dev/null || fail "docker is not installed. This is the blocker; everything else waits on it."
docker info >/dev/null 2>&1 || fail "the docker daemon is not running. Start Docker Desktop."
ok "docker daemon is up"

[ -f deploy/certs/fullchain.pem ] || fail "no certificate. Run: make tls-cert"
ok "certificate present"

for name in jwt_secret postgres_password database_url google_api_key tavily_api_key; do
    [ -f "deploy/secrets/$name" ] || fail "deploy/secrets/$name is missing. Run: make secrets"
done
for name in google_api_key tavily_api_key; do
    [ -s "deploy/secrets/$name" ] || fail "deploy/secrets/$name is empty. Put your key in it:
    printf '%s' 'YOUR_KEY' > deploy/secrets/$name"
done
ok "all five secrets present, and the two API keys are not empty"

# ---------------------------------------------------------------------------
step "Starting the stack"
$COMPOSE up --build -d

# The API's healthcheck already waits on postgres, redis and a completed
# migration, so waiting on the API is waiting on all of it.
printf 'waiting for the API to report healthy '
for _ in $(seq 1 60); do
    state=$($COMPOSE ps --format json api 2>/dev/null | grep -o '"Health":"[a-z]*"' | head -1 || true)
    case "$state" in
        *healthy*) printf '\n'; ok "API is healthy"; break ;;
    esac
    printf '.'
    sleep 5
done
case "${state:-}" in
    *healthy*) ;;
    *) printf '\n'; $COMPOSE ps; fail "the API never became healthy. Look at: make logs" ;;
esac

# ---------------------------------------------------------------------------
step "Every service is healthy"
# Not just the API, and "starting" is not "healthy". The first passing run of
# this script reported success while the worker was crash-looping on a missing
# libpq and the web container could never pass its health check -- because it
# only ever asked about the API. A verification that checks one service out of
# five can pass while the product does nothing.
#
# Every service in this stack declares a health check, so every one of them is
# waited on. Accepting "health: starting" would reintroduce the same hole a
# few seconds earlier.
for service in postgres redis api web worker; do
    for _ in $(seq 1 36); do
        status=$($COMPOSE ps --format '{{.Service}} {{.Status}}' 2>/dev/null | grep "^$service " || true)
        case "$status" in
            *"(healthy)"*)   break ;;
            *Restarting*|*Exit*)
                $COMPOSE logs --tail 25 "$service" 2>&1 | tail -25
                fail "$service is not running: $status" ;;
        esac
        sleep 5
    done
    case "$status" in
        *"(healthy)"*) ok "$service is healthy" ;;
        *) $COMPOSE logs --tail 25 "$service" 2>&1 | tail -25
           fail "$service never became healthy: ${status:-absent}" ;;
    esac
done

# ---------------------------------------------------------------------------
step "Migrations ran to completion"
migrate_exit=$($COMPOSE ps -a --format json migrate | grep -o '"ExitCode":[0-9]*' | head -1 | cut -d: -f2)
[ "${migrate_exit:-1}" = "0" ] || fail "the migration job exited ${migrate_exit:-?}, not 0"
ok "schema applied, job exited 0"

# ---------------------------------------------------------------------------
step "TLS"
$CURL -o /dev/null -w '%{http_code}' "$BASE/" | grep -q '^200$' \
    || fail "$BASE did not answer 200"
ok "8443 serves over TLS"

redirect=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://localhost:${HTTP_PORT:-8080}/")
[ "$redirect" = "301" ] || fail "plain HTTP answered $redirect, not 301. It must redirect and serve nothing."
ok "8080 redirects with 301 and serves nothing"

# The header that only exists over TLS, and the one whose absence on assets
# was the bug found while writing M26.
$CURL -I "$BASE/" | grep -qi 'strict-transport-security' || fail "no HSTS header over TLS"
ok "HSTS is present over TLS"

asset=$($CURL "$BASE/" | grep -o '/assets/[A-Za-z0-9._-]*\.js' | head -1 || true)
if [ -n "$asset" ]; then
    $CURL -I "$BASE$asset" | grep -qi 'content-security-policy' \
        || fail "$asset is served without a CSP -- the add_header inheritance bug is back"
    ok "assets carry the content-security policy"
fi

# ---------------------------------------------------------------------------
step "An account, and a request through the whole stack"

if [ ! -f "$ACCOUNT_FILE" ]; then
    password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"

    # getpass has no tty under `exec -T`, so it falls back to stdin. The prompt
    # asks twice.
    create() {
        printf '%s\n%s\n' "$password" "$password" \
            | $COMPOSE exec -T api python -m core.cli users create "$1" 2>&1
    }

    # `|| true` on every capture: under `set -e` a failing command substitution
    # kills the script before the case below can read what went wrong, so the
    # "already exists" path was unreachable and the failure printed nothing at
    # all.
    result=$(create "$EMAIL") || true
    case "$result" in
        *"already exists"*)
            # The database volume outlives `down`, so an account from an
            # earlier run is still there while its password file is not --
            # and the password cannot be recovered. A fresh address is the
            # honest way through: the old account is untouched rather than
            # reset behind the operator's back.
            EMAIL="verify+$(python3 -c 'import secrets;print(secrets.token_hex(4))')@example.com"
            result=$(create "$EMAIL") || true
            ;;
    esac
    case "$result" in
        *created*) ;;
        *) printf '%s\n' "$result"
           fail "could not create an account" ;;
    esac

    printf '%s\n%s\n' "$EMAIL" "$password" > "$ACCOUNT_FILE"
    chmod 644 "$ACCOUNT_FILE"
    ok "created $EMAIL (credentials in $ACCOUNT_FILE)"
else
    EMAIL=$(sed -n 1p "$ACCOUNT_FILE")
    ok "reusing $EMAIL from $ACCOUNT_FILE"
fi

password=$(sed -n 2p "$ACCOUNT_FILE")

login=$($CURL -X POST "$BASE/api/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"$EMAIL\",\"password\":\"$password\"}") || true
token=$(printf '%s' "$login" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])' 2>/dev/null) || true
# The response is shown on failure. A bare "sign-in failed" sent the last three
# diagnoses back to a manual curl to find out why.
[ -n "$token" ] || fail "sign-in failed through the proxy. The API said: $login"
ok "signed in over TLS, through nginx, to the API"

code=$($CURL -o /tmp/dt-submit.json -w '%{http_code}' -X POST "$BASE/api/research" \
    -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $token" \
    -d '{"question":"What is the CAP theorem and what does it actually constrain?","depth":"quick"}')
[ "$code" = "202" ] || fail "submit answered $code, not 202. Body: $(cat /tmp/dt-submit.json)"
ok "a research job was accepted (202) and queued"

# ---------------------------------------------------------------------------
step "The worker actually picked the job up"
# Accepting a job proves the API and the queue work. It does not prove anything
# runs it -- and a crash-looping worker leaves the request sitting in Redis
# while every check above still passes.
research_id=$(python3 -c 'import json;print(json.load(open("/tmp/dt-submit.json"))["research_id"])' 2>/dev/null || true)
[ -n "$research_id" ] || fail "the submit response carried no research id"

printf 'waiting for the worker to start it '
for _ in $(seq 1 24); do
    if $COMPOSE logs worker 2>/dev/null | grep -q "$research_id"; then
        printf '\n'; ok "the worker began $research_id"; started=yes; break
    fi
    $COMPOSE ps --format '{{.Service}} {{.Status}}' 2>/dev/null | grep -q '^worker.*Restarting' \
        && { printf '\n'; $COMPOSE logs --tail 20 worker; fail "the worker is crash-looping"; }
    printf '.'
    sleep 5
done
[ "${started:-}" = "yes" ] || { printf '\n'; fail "the worker never touched $research_id in two minutes"; }

# ---------------------------------------------------------------------------
printf '\n\033[32mThe stack is up and serving over TLS.\033[0m\n\n'
printf '  Open      %s\n' "$BASE"
printf '  Sign in   %s  (password in %s)\n' "$EMAIL" "$ACCOUNT_FILE"
printf '  Logs      make logs\n'
printf '  Stop      make down-deploy\n\n'
printf 'The browser will refuse the self-signed certificate. That is correct.\n'
printf 'A worker is now running that question; it spends about 7 of the free\n'
printf "tier's 20 daily model requests.\n"
