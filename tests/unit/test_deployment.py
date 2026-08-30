"""Checks on the container setup that do not need a container runtime.

The compose stack cannot be built here -- there is no Docker daemon on the
development machine -- so it would be easy to let these files drift into
plausible-looking fiction. These are the parts that can be checked anyway, and
they are the parts that actually break: names that must match something real,
and ordering that must be expressed as a condition rather than a hope.

What they cannot check is that an image builds or that the stack comes up. That
is stated in DEPLOYMENT rather than implied by a green suite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from core.config import Settings

ROOT = Path(__file__).resolve().parents[2]

CONTAINER_ONLY = {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"}
"""Variables the postgres image reads, which are not application settings."""


SERVER_FILES = ("nginx.conf", "nginx.tls.conf")
"""The two nginx entry points: plain HTTP locally and in CI, TLS in a
deployment. Both include the same two snippets."""


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


@pytest.fixture(scope="module")
def deploy_compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.deploy.yml").read_text())


@pytest.fixture(scope="module")
def app_snippet() -> str:
    return (ROOT / "apps/web/snippets/app.conf").read_text()


@pytest.fixture(scope="module")
def headers_snippet() -> str:
    return (ROOT / "apps/web/snippets/security-headers.conf").read_text()


def _in_build_context(path: str) -> bool:
    """Whether `.dockerignore` lets a path reach the build.

    Docker applies every pattern and the **last** match wins, which is what
    makes `scripts/` followed by `!scripts/serve-with-worker.sh` mean "none of
    it except this". Implemented rather than eyeballed because the failure it
    catches -- a COPY that finds nothing -- reports itself as a build error
    with no mention of .dockerignore at all.
    """
    import fnmatch

    included = True
    for line in (ROOT / ".dockerignore").read_text().splitlines():
        rule = line.strip()
        if not rule or rule.startswith("#"):
            continue
        negated = rule.startswith("!")
        pattern = rule.lstrip("!").rstrip("/")
        if fnmatch.fnmatch(path, pattern) or path.startswith(f"{pattern}/"):
            included = negated
    return included


class TestTheComposeFile:
    def test_it_parses(self, compose: dict) -> None:
        """Not a formality. The first version of this file did not parse: an
        unquoted error message containing a colon-space was read as a nested
        mapping, and nothing but a parser would have noticed."""
        assert isinstance(compose, dict)

    def test_it_declares_the_five_services_and_one_job(self, compose: dict) -> None:
        assert set(compose["services"]) == {
            "postgres",
            "redis",
            "migrate",
            "api",
            "worker",
            "web",
        }

    @pytest.mark.parametrize("service", ["postgres", "redis", "api", "worker", "web"])
    def test_every_long_running_service_has_a_health_check(
        self, compose: dict, service: str
    ) -> None:
        assert "healthcheck" in compose["services"][service]

    def test_the_migration_job_has_none(self, compose: dict) -> None:
        """It is a job, not a service. A health check on something that is
        supposed to exit would report a successful migration as unhealthy."""
        assert "healthcheck" not in compose["services"]["migrate"]

    def test_the_migration_job_does_not_restart(self, compose: dict) -> None:
        """A failing migration is something a person has to read. Restarting it
        buries the error in a loop."""
        assert compose["services"]["migrate"]["restart"] == "no"


class TestStartupOrdering:
    """`depends_on` alone waits for a container to start, not to be usable.

    PostgreSQL's process exists well before it will accept a connection, so a
    bare dependency produces an API that connects, fails, and restarts -- a loop
    that looks like a broken application rather than a race.
    """

    @pytest.mark.parametrize("service", ["migrate", "api", "worker", "web"])
    def test_dependencies_wait_on_a_condition(self, compose: dict, service: str) -> None:
        depends = compose["services"][service]["depends_on"]

        assert isinstance(depends, dict), f"{service} uses the bare list form"
        assert all("condition" in value for value in depends.values())

    @pytest.mark.parametrize("service", ["api", "worker"])
    def test_the_schema_exists_before_anything_uses_it(self, compose: dict, service: str) -> None:
        depends = compose["services"][service]["depends_on"]

        assert depends["migrate"]["condition"] == "service_completed_successfully"
        assert depends["postgres"]["condition"] == "service_healthy"
        assert depends["redis"]["condition"] == "service_healthy"


class TestExposure:
    def test_only_the_web_service_publishes_a_port(self, compose: dict) -> None:
        """The database, Redis and the API are reachable on the compose network
        and nowhere else, so the API cannot be called around the proxy and the
        database is not on the host's interface."""
        published = {name for name, service in compose["services"].items() if service.get("ports")}

        assert published == {"web"}

    def test_no_secret_has_a_baked_in_default(self, compose: dict) -> None:
        """`${VAR:-something}` would start the stack with a signing key every
        deployment shares, which is worse than not starting.

        Empty is allowed, and the `${VAR:?}` guard that used to be here is not.
        It could only guard one way of starting the application, and compose
        interpolates each file before merging an overlay -- so requiring the
        variable here would also require it from a deployment that has replaced
        it with a mounted file. The requirement moved into Settings, where it
        covers every deployment method and is a test rather than a convention;
        see `TestProductionRefusesToStartWithoutCredentials` in test_config.
        """
        environment = compose["services"]["api"]["environment"]

        for name in ("JWT_SECRET", "GOOGLE_API_KEY", "TAVILY_API_KEY"):
            value = str(environment[name])
            assert value.endswith(":-}"), f"{name} is {value!r}, which is not an empty default"


class TestTheVercelConfig:
    """The static host serving the client in a split deployment.

    Its headers have to say something different from nginx's, because the API
    is no longer the page's own origin -- and getting that wrong fails only in
    a browser, where no test here runs.
    """

    @pytest.fixture(scope="class")
    def vercel(self) -> dict:
        import json

        return json.loads((ROOT / "apps/web/vercel.json").read_text())

    def _csp(self, vercel: dict) -> str:
        for block in vercel["headers"]:
            for header in block["headers"]:
                if header["key"] == "Content-Security-Policy":
                    return str(header["value"])
        raise AssertionError("no Content-Security-Policy")

    def test_the_policy_allows_the_api_it_is_built_against(self, vercel: dict) -> None:
        """`connect-src 'self'` -- which is exactly what nginx serves, and the
        obvious thing to copy -- blocks every call in a split deployment,
        because the API is a different origin. The page loads perfectly and
        nothing works."""
        connect = self._csp(vercel).split("connect-src")[1].split(";")[0]

        assert "https://" in connect, "connect-src names no API host"
        assert "wss://" in connect, (
            "connect-src allows https but not wss, so REST works and the "
            "progress stream is blocked -- which reads as a broken feature "
            "rather than a policy"
        )

    def test_both_schemes_name_the_same_host(self, vercel: dict) -> None:
        """A policy that permits one host over https and a different one over
        wss is a typo that presents as an intermittent bug."""
        connect = self._csp(vercel).split("connect-src")[1].split(";")[0]
        hosts = {
            token.split("://", 1)[1].rstrip("/") for token in connect.split() if "://" in token
        }

        assert len(hosts) == 1, f"connect-src names more than one host: {hosts}"

    def test_it_is_otherwise_as_strict_as_the_nginx_policy(
        self, vercel: dict, headers_snippet: str
    ) -> None:
        """Everything except connect-src should match what the containerised
        deployment serves. A split is a reason to widen one directive, not the
        others."""
        csp = self._csp(vercel)

        for directive in (
            "default-src 'self'",
            "script-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'self'",
        ):
            assert directive in csp, f"{directive} is missing"
            assert directive in headers_snippet, f"{directive} is not what nginx serves"


class TestTheRenderBlueprint:
    """The free-tier shape: one web service running both processes.

    Nothing here has been deployed, so these checks are the only thing standing
    between a mistake and a platform build minutes long.
    """

    @pytest.fixture(scope="class")
    def render(self) -> dict:
        return yaml.safe_load((ROOT / "render.yaml").read_text())

    def test_the_command_names_a_file_the_image_contains(self, render: dict) -> None:
        """Three things have to agree, and the first version of this test only
        checked two.

        The file must exist, the Dockerfile must COPY it, **and
        `.dockerignore` must not exclude it from the build context**. Missing
        that third one is why this test passed while the deploy failed after
        eleven seconds with "no source files were found" -- an assertion that
        confirmed the intention rather than checking the outcome.
        """
        web = next(s for s in render["services"] if s["type"] == "web")
        script = web["dockerCommand"].split()[-1]

        assert (ROOT / script).exists(), f"{script} does not exist"

        dockerfile = (ROOT / "Dockerfile").read_text()
        roots = {
            line.split()[-2].rstrip("/")
            for line in dockerfile.splitlines()
            if line.startswith("COPY") and "--from" not in line
        }
        assert script.split("/")[0] in roots, (
            f"the image does not COPY {script.split('/')[0]}/, so the command cannot run"
        )

        assert _in_build_context(script), (
            f".dockerignore excludes {script} from the build context, so the COPY "
            f"finds nothing and the build fails before it starts"
        )

    def test_the_secrets_are_not_in_the_file(self, render: dict) -> None:
        """A blueprint is committed. Anything real in it is real in a public
        repository."""
        web = next(s for s in render["services"] if s["type"] == "web")
        by_key = {entry["key"]: entry for entry in web["envVars"]}

        for key in ("GOOGLE_API_KEY", "TAVILY_API_KEY", "CORS_ORIGINS"):
            assert by_key[key].get("sync") is False, f"{key} must be entered by hand"
        assert by_key["JWT_SECRET"].get("generateValue") is True

    def test_every_environment_key_is_a_real_setting(self, render: dict) -> None:
        """The same check the compose file gets. A misspelled variable produces
        a service that starts, ignores the setting, and behaves subtly
        differently from every other environment."""
        known = {name.upper() for name in Settings.model_fields}
        web = next(s for s in render["services"] if s["type"] == "web")

        unknown = [
            entry["key"]
            for entry in web["envVars"]
            if entry["key"] not in known and entry["key"] not in CONTAINER_ONLY
        ]
        assert not unknown, f"render.yaml sets {unknown}, which Settings does not read"

    def test_the_strong_tier_is_not_the_model_that_stopped_answering(self, render: dict) -> None:
        """`gemini-3.7-flash` accepts requests and never replies. A default
        naming it is a deployment that researches, collects sources, and
        returns nothing."""
        web = next(s for s in render["services"] if s["type"] == "web")
        by_key = {entry["key"]: entry.get("value") for entry in web["envVars"]}

        assert by_key["LLM_MODEL_STRONG"] != "gemini-3.7-flash"


class TestTheDeploymentOverlay:
    """The overlay that turns the stack into something exposable: TLS at the
    edge, and every secret read from a file rather than the environment."""

    def test_every_secret_is_supplied_as_a_file(self, deploy_compose: dict) -> None:
        api = deploy_compose["services"]["api"]["environment"]

        for name in ("JWT_SECRET", "GOOGLE_API_KEY", "TAVILY_API_KEY", "DATABASE_URL"):
            assert api[f"{name}_FILE"], f"{name} has no file"
            # Explicitly "" and **never** null. This assertion used to demand
            # null, and was wrong in exactly the way the overlay was wrong --
            # both written from the same belief that a bare `KEY:` removes a
            # variable. It does not: it means "pass this through from the
            # environment", and compose's environment includes the project's
            # `.env`. The overlay pulled a developer's local DATABASE_URL into
            # the production container, and the test agreed with it.
            assert api[name] == "", (
                f"{name} is {api[name]!r}. A bare `KEY:` inherits from .env; "
                f"only an explicit empty string clears it."
            )

    def test_no_environment_key_is_left_null(self, deploy_compose: dict) -> None:
        """A null value anywhere in this file inherits from the environment,
        and compose's environment includes `.env`. That is never what an
        overlay whose purpose is to *replace* configuration wants -- so the
        rule is checked across every service, not just the ones that happen to
        carry secrets today."""
        for name, service in deploy_compose["services"].items():
            environment = service.get("environment") or {}
            if not isinstance(environment, dict):
                continue
            null_keys = [key for key, value in environment.items() if value is None]
            assert not null_keys, (
                f"{name} leaves {null_keys} null, which inherits from .env rather than clearing it"
            )

    def test_every_named_secret_is_declared(self, deploy_compose: dict) -> None:
        """A service referencing a secret the file does not define is a compose
        error at `up`, which is late for something a parser can see."""
        declared = set(deploy_compose["secrets"])

        for name, service in deploy_compose["services"].items():
            for used in service.get("secrets") or []:
                assert used in declared, f"{name} uses undeclared secret {used}"

    def test_every_declared_secret_is_used(self, deploy_compose: dict) -> None:
        """The mirror. An unused declaration is a file somebody is maintaining
        for nothing, or a mount that was meant to be wired and was not."""
        used = {
            name
            for service in deploy_compose["services"].values()
            for name in (service.get("secrets") or [])
        }

        assert set(deploy_compose["secrets"]) == used

    def test_the_secret_file_paths_match_where_compose_mounts_them(
        self, deploy_compose: dict
    ) -> None:
        """Compose mounts a secret at /run/secrets/<name>. A *_FILE pointing
        anywhere else names a path that will not exist, and the application
        refuses to start rather than starting without the credential -- correct,
        but a confusing way to learn about a typo."""
        for service in deploy_compose["services"].values():
            environment = service.get("environment") or {}
            declared = set(service.get("secrets") or [])
            for key, value in environment.items():
                if not key.endswith("_FILE") or not value:
                    continue
                assert str(value).startswith("/run/secrets/"), value
                assert Path(str(value)).name in declared, f"{value} is not mounted here"

    def test_the_api_trusts_only_the_compose_subnet_for_forwarded_headers(
        self, deploy_compose: dict
    ) -> None:
        """`*` would let any client claim any address and get a fresh
        rate-limit bucket per request, which is not a rate limit."""
        command = deploy_compose["services"]["api"]["command"]
        trusted = command[command.index("--forwarded-allow-ips") + 1]

        assert trusted != "*"
        subnet = deploy_compose["networks"]["default"]["ipam"]["config"][0]["subnet"]
        assert trusted == subnet

    def test_the_edge_publishes_both_ports(self, deploy_compose: dict) -> None:
        """8443 serves, and 8080 exists only to redirect to it. Closing 8080
        would leave a client that typed the bare hostname with a refused
        connection rather than a working link."""
        ports = " ".join(deploy_compose["services"]["web"]["ports"])

        assert ":8443" in ports
        assert ":8080" in ports

    def test_certificates_are_mounted_read_only(self, deploy_compose: dict) -> None:
        mounts = deploy_compose["services"]["web"]["volumes"]

        certs = [m for m in mounts if "/etc/nginx/certs" in m]
        assert certs and all(m.endswith(":ro") for m in certs)

    def test_secrets_and_certificates_are_not_committed(self) -> None:
        """The one mistake in this milestone that cannot be undone by a later
        commit: git remembers."""
        ignore = (ROOT / ".gitignore").read_text()

        assert "deploy/secrets/*" in ignore
        assert "deploy/certs/*" in ignore


class TestNamesThatMustMatchSomethingReal:
    """The bug class that static checking actually catches.

    A misspelled environment variable does not fail a build. It produces a
    container that starts, ignores the setting, and behaves subtly differently
    from every other environment.
    """

    def test_every_environment_key_is_a_real_setting(self, compose: dict) -> None:
        known = {name.upper() for name in Settings.model_fields}

        for name, service in compose["services"].items():
            environment = service.get("environment") or {}
            if not isinstance(environment, dict):
                continue
            unknown = [key for key in environment if key not in known and key not in CONTAINER_ONLY]
            assert not unknown, f"{name} sets {unknown}, which Settings does not read"

    def test_every_copied_path_exists(self) -> None:
        """A COPY of a path that is not there fails the build -- but only when
        somebody has a daemon to run it on."""
        for line in (ROOT / "Dockerfile").read_text().splitlines():
            match = re.match(r"COPY (?:--from=\S+ |--chown=\S+ )*(\S+) ", line)
            if match and not match.group(1).startswith("/"):
                assert (ROOT / match.group(1)).exists(), match.group(1)

    def test_the_web_image_has_what_it_needs(self) -> None:
        """`npm ci` requires a lockfile and fails outright without one."""
        assert (ROOT / "apps/web/package-lock.json").exists()
        assert (ROOT / "apps/web/nginx.conf").exists()

    def test_the_build_context_excludes_secrets(self) -> None:
        """Anything in the context can end up in a layer, and a layer is
        readable by anyone who can pull the image."""
        ignored = (ROOT / ".dockerignore").read_text()

        assert ".env" in ignored


class TestTheProxy:
    """The proxy rules live in one snippet included by both server files.

    They were duplicated until the TLS file existed, at which point two copies
    had to stay in step by hand -- and the thing they govern, a WebSocket that
    only breaks under TLS, is exactly the kind of difference nobody notices
    until a deployment.
    """

    def test_the_websocket_upgrade_is_configured(self, app_snippet: str) -> None:
        """Without these headers the progress stream fails at the handshake
        while every other endpoint works perfectly -- a spinner that never
        moves, and nothing in the API logs to explain it."""
        assert "proxy_http_version 1.1" in app_snippet
        assert "proxy_set_header Upgrade $http_upgrade" in app_snippet
        assert 'proxy_set_header Connection "upgrade"' in app_snippet

    def test_the_read_timeout_outlives_a_research_run(self, app_snippet: str) -> None:
        """nginx defaults to sixty seconds. A run is quiet for far longer than
        that between model calls, so the default closes a healthy stream."""
        timeout = re.search(r"proxy_read_timeout (\d+)s", app_snippet)

        assert timeout is not None
        assert int(timeout.group(1)) >= 600

    def test_client_routes_fall_back_to_the_app(self, app_snippet: str) -> None:
        """Reloading /research/res_abc is a client route, not a missing file."""
        assert "try_files $uri $uri/ /index.html" in app_snippet

    @pytest.mark.parametrize("server_file", SERVER_FILES)
    def test_both_server_files_include_the_shared_rules(self, server_file: str) -> None:
        """The plain and TLS files must serve the same application. Sharing the
        snippet is what makes that true by construction rather than by review."""
        config = (ROOT / "apps/web" / server_file).read_text()

        assert "include /etc/nginx/snippets/app.conf;" in config
        assert "include /etc/nginx/snippets/security-headers.conf;" in config


class TestSecurityHeadersReachEveryResponse:
    """nginx inherits `add_header` from an outer level *only* when the current
    level defines none of its own.

    That rule cost this configuration its content-security policy on every
    script and stylesheet it serves: `location /assets/` set Cache-Control, and
    silently dropped four security headers it never mentioned. The server block
    still listed them, so nothing read as wrong.
    """

    def test_every_location_that_sets_a_header_re_includes_them(self, app_snippet: str) -> None:
        blocks = re.split(r"\nlocation ", app_snippet)

        for block in blocks[1:]:
            name = block.split("{")[0].strip()
            if "add_header" not in block:
                continue  # inherits from the server level, which is correct
            assert "include /etc/nginx/snippets/security-headers.conf;" in block, (
                f"location {name} sets add_header, so nginx stops inheriting the "
                f"security headers -- it must include them itself"
            )

    def test_the_policy_is_actually_restrictive(self, headers_snippet: str) -> None:
        assert "default-src 'self'" in headers_snippet
        assert "frame-ancestors 'none'" in headers_snippet
        # The one place 'unsafe-inline' is allowed, and only for styles.
        assert "script-src 'self';" in headers_snippet


class TestTls:
    def test_plain_http_only_redirects(self) -> None:
        """A stack that answers on both schemes is one where a client that
        forgot the scheme keeps working, so nobody finds out the credential
        travelled in clear."""
        config = (ROOT / "apps/web/nginx.tls.conf").read_text()
        plain = config.split("listen 8080;")[1].split("server {")[0]

        assert "return 301 https://$host$request_uri;" in plain
        assert "proxy_pass" not in plain

    def test_the_health_path_is_not_swallowed_by_the_redirect(self) -> None:
        """A server-level `return` runs in the rewrite phase, before nginx
        selects a location -- so it beats even an exact `location =` match.

        Written the obvious way, the health path was dead configuration: every
        probe was redirected to port 443, nothing listens there inside the
        container, and the container reported unhealthy while serving the
        internet correctly. The redirect has to sit in `location /` for the
        exact match to win, and nothing static can see the difference except
        this.
        """
        config = (ROOT / "apps/web/nginx.tls.conf").read_text()
        plain = config.split("listen 8080;")[1].split("listen 8443")[0]

        assert "location = /healthz" in plain
        assert "location / {" in plain
        # The redirect must be *inside* a location block. At server level it
        # would pre-empt the health path again.
        for line in plain.splitlines():
            stripped = line.strip()
            if stripped.startswith("return 301"):
                assert line.startswith("        "), (
                    "the redirect is at server level, where it runs before "
                    "location matching and swallows /healthz"
                )

    def test_only_modern_tls_is_negotiable(self) -> None:
        config = (ROOT / "apps/web/nginx.tls.conf").read_text()

        assert "ssl_protocols TLSv1.2 TLSv1.3;" in config
        assert "TLSv1.1" not in config
        assert "TLSv1 " not in config

    def test_session_tickets_are_off(self) -> None:
        """nginx generates the ticket key at start and never rotates it, so a
        ticket recovered later decrypts a session recorded earlier."""
        assert "ssl_session_tickets off;" in (ROOT / "apps/web/nginx.tls.conf").read_text()

    def test_hsts_is_sent_only_over_tls(self) -> None:
        """The header is emitted from $hsts_header, which each server file maps
        from the scheme. Over plain HTTP it is the empty string, and nginx omits
        an add_header with an empty value."""
        assert "$hsts_header" in (ROOT / "apps/web/snippets/security-headers.conf").read_text()

        plain = (ROOT / "apps/web/nginx.conf").read_text()
        assert "map $scheme $hsts_header" in plain
        assert "max-age" not in plain

        tls = (ROOT / "apps/web/nginx.tls.conf").read_text()
        assert re.search(r"https\s+\"max-age=\d+", tls) is not None

    def test_hsts_does_not_preload(self) -> None:
        """Preload is a submission to a list compiled into browser binaries,
        and getting off it takes months. Right end state, wrong thing to enable
        in the commit that first serves a certificate."""
        tls = (ROOT / "apps/web/nginx.tls.conf").read_text()
        value = re.search(r'https\s+"(max-age=[^"]*)"', tls)

        assert value is not None
        assert "preload" not in value.group(1)

    def test_the_image_ships_both_server_files_and_the_snippets(self) -> None:
        """The TLS file is mounted over the default at deploy time, but it is
        built into the image so what gets promoted is what was tested."""
        dockerfile = (ROOT / "apps/web/Dockerfile").read_text()

        assert "COPY snippets/ /etc/nginx/snippets/" in dockerfile
        assert "nginx.tls.conf" in dockerfile


class TestTheWorkflow:
    """Checks on CI that do not need a CI runner.

    A workflow cannot be executed here, but the parts that break are checkable:
    that it parses, that it does not need write access, and that the commands it
    runs are commands this repository actually has. The last one is the reason
    this class exists -- a workflow referencing a make target that was renamed
    fails on the first push, which is the worst possible time to find out.
    """

    @pytest.fixture(scope="class")
    def workflow(self) -> dict:
        return yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())

    def test_it_parses(self, workflow: dict) -> None:
        assert isinstance(workflow, dict)

    def test_it_has_the_four_jobs(self, workflow: dict) -> None:
        assert set(workflow["jobs"]) == {"check", "integration", "web", "images"}

    def test_it_only_asks_for_read_access(self, workflow: dict) -> None:
        """Nothing here publishes, comments, or pushes. A job with write access
        is a much larger thing than a job with a complaint, and the default
        token is generous unless told otherwise."""
        assert workflow["permissions"] == {"contents": "read"}

    def test_superseded_runs_are_cancelled(self, workflow: dict) -> None:
        assert workflow["concurrency"]["cancel-in-progress"] is True

    def test_every_job_has_a_timeout(self, workflow: dict) -> None:
        """A hung job holds a runner until GitHub's six-hour default expires."""
        for name, job in workflow["jobs"].items():
            assert job.get("timeout-minutes"), name

    def test_no_job_runs_the_paid_tests(self, workflow: dict) -> None:
        """The `llm` marker costs money on every push. A CI run that spends is
        a CI run that gets switched off."""
        text = (ROOT / ".github/workflows/ci.yml").read_text()

        assert "-m llm" not in text
        assert "not llm" in text

    def test_service_containers_wait_to_be_healthy(self, workflow: dict) -> None:
        """Without a health check the job starts before PostgreSQL accepts
        connections, and the first test fails on a race rather than on
        anything real -- the same trap the compose file avoids."""
        services = workflow["jobs"]["integration"]["services"]

        for name, service in services.items():
            assert "--health-cmd" in service["options"], name

    def test_ci_secrets_are_obvious_placeholders(self, workflow: dict) -> None:
        """No integration test makes a provider call, so these exist only to
        satisfy the settings object. A real key in a workflow file is a real
        key in a public repository."""
        env = workflow["jobs"]["integration"]["env"]

        assert "not-a-real-key" in env["GOOGLE_API_KEY"]
        assert "not-a-real-key" in env["TAVILY_API_KEY"]

    def test_ci_parses_both_nginx_configurations(self, workflow: dict) -> None:
        """There is no nginx on this machine, so this CI step is the only thing
        that ever reads either file with the program that has to read it.
        Deleting it would leave the TLS configuration unverified while the suite
        stayed green, which is the failure mode this whole test file exists for.
        """
        steps = workflow["jobs"]["images"]["steps"]
        run = "\n".join(str(step.get("run", "")) for step in steps)

        assert "nginx -t" in run
        assert "nginx.tls.conf" in run
        # Against a real certificate: nginx -t opens the key, so a config that
        # names it wrongly passes a syntax check and fails at deploy.
        assert "deploy/certs" in run

    def test_ci_resolves_the_deployment_overlay(self, workflow: dict) -> None:
        steps = workflow["jobs"]["images"]["steps"]
        run = "\n".join(str(step.get("run", "")) for step in steps)

        assert "docker-compose.deploy.yml config" in run

    def test_images_are_built_but_never_pushed(self, workflow: dict) -> None:
        """Proving an image builds needs no credentials. Publishing one does,
        and this workflow should not have them."""
        steps = workflow["jobs"]["images"]["steps"]
        builds = [step for step in steps if "build-push-action" in str(step.get("uses", ""))]

        assert builds
        for step in builds:
            assert step["with"]["push"] is False


class TestMakeMatchesCI:
    def test_the_check_target_runs_what_ci_runs(self) -> None:
        """`make check` is documented as "everything CI runs", and for a while
        it was not: CI enforced formatting and the target did not, so the first
        push would have gone red on a check that passed locally. A claim in a
        help string is still a claim.
        """
        makefile = (ROOT / "Makefile").read_text()
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()

        assert "format --check" in workflow
        assert "format --check" in makefile
