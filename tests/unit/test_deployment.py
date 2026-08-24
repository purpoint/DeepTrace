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


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


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

    def test_secrets_have_no_defaults(self, compose: dict) -> None:
        """`${VAR:?message}` fails the stack with an explanation. `${VAR:-x}`
        would start it with a shared signing key, which is worse than not
        starting."""
        environment = compose["services"]["api"]["environment"]

        for name in ("JWT_SECRET", "GOOGLE_API_KEY", "TAVILY_API_KEY"):
            assert ":?" in str(environment[name]), name


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
    def test_the_websocket_upgrade_is_configured(self) -> None:
        """Without these headers the progress stream fails at the handshake
        while every other endpoint works perfectly -- a spinner that never
        moves, and nothing in the API logs to explain it."""
        config = (ROOT / "apps/web/nginx.conf").read_text()

        assert "proxy_http_version 1.1" in config
        assert "proxy_set_header Upgrade $http_upgrade" in config
        assert 'proxy_set_header Connection "upgrade"' in config

    def test_the_read_timeout_outlives_a_research_run(self) -> None:
        """nginx defaults to sixty seconds. A run is quiet for far longer than
        that between model calls, so the default closes a healthy stream."""
        config = (ROOT / "apps/web/nginx.conf").read_text()
        timeout = re.search(r"proxy_read_timeout (\d+)s", config)

        assert timeout is not None
        assert int(timeout.group(1)) >= 600

    def test_client_routes_fall_back_to_the_app(self) -> None:
        """Reloading /research/res_abc is a client route, not a missing file."""
        assert "try_files $uri $uri/ /index.html" in (ROOT / "apps/web/nginx.conf").read_text()


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
