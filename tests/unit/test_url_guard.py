"""SSRF protection tests.

This is a security control, so it is tested as an attacker would probe it: one
case per bypass technique, each named after the technique rather than the code
path. A test that only checks the happy path proves nothing about a guard.

No network access. Every hostile case resolves locally or is rejected before
resolution, and the two positive cases use addresses that need no DNS.
"""

from __future__ import annotations

import pytest

from core.tools.url_guard import (
    ALLOWED_PORTS,
    ALLOWED_SCHEMES,
    BLOCKED_ADDRESSES,
    URLValidationError,
    _check_address,
    is_safe_url,
    validate_url,
)

pytestmark = pytest.mark.unit


def assert_blocked(url: str, *, because: str = "") -> URLValidationError:
    with pytest.raises(URLValidationError) as exc:
        validate_url(url)
    if because:
        assert because in exc.value.reason.lower()
    return exc.value


class TestProtocolRestriction:
    """A non-HTTP scheme turns the fetcher into a general-purpose client."""

    def test_file_scheme_cannot_read_local_disk(self) -> None:
        assert_blocked("file:///etc/passwd", because="scheme")

    @pytest.mark.parametrize(
        "url",
        [
            "gopher://internal:6379/_FLUSHALL",
            "dict://internal:11211/stats",
            "ftp://internal/secrets",
            "data:text/html,<script>x</script>",
            "jar:http://example.com!/",
        ],
    )
    def test_protocol_smuggling_schemes_are_refused(self, url: str) -> None:
        assert_blocked(url, because="scheme")

    def test_only_http_and_https_are_permitted(self) -> None:
        assert {"http", "https"} == ALLOWED_SCHEMES


class TestCloudMetadataEndpoints:
    """The highest-value SSRF target: instance credentials."""

    def test_aws_imds_by_address(self) -> None:
        error = assert_blocked("http://169.254.169.254/latest/meta-data/iam/")
        assert "metadata" in error.reason.lower()

    @pytest.mark.parametrize(
        "url",
        [
            "http://metadata.google.internal/computeMetadata/v1/",
            "http://metadata.goog/",
            "http://instance-data/latest/meta-data/",
        ],
    )
    def test_metadata_hostnames_are_blocked_by_name(self, url: str) -> None:
        """Defence in depth: a split-horizon resolver could make these resolve
        to something that passes an address check."""
        assert_blocked(url, because="blocked hostname")

    @pytest.mark.parametrize("address", sorted(BLOCKED_ADDRESSES))
    def test_every_listed_metadata_address_is_rejected(self, address: str) -> None:
        with pytest.raises(URLValidationError):
            _check_address(address, url=f"http://{address}/")


class TestPrivateAddressRanges:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("http://127.0.0.1:8080/admin", "loopback"),
            ("http://127.1.2.3/", "loopback"),
            ("http://10.0.0.5/internal", "private"),
            ("http://192.168.1.1/router", "private"),
            ("http://172.16.0.1/", "private"),
            ("http://169.254.1.1/", "link-local"),
            ("http://0.0.0.0/", "unspecified"),
            ("http://224.0.0.1/", "multicast"),
        ],
    )
    def test_non_public_ipv4_is_rejected(self, url: str, expected: str) -> None:
        assert_blocked(url, because=expected)

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("http://[::1]/admin", "loopback"),
            ("http://[fe80::1]/", "link-local"),
            ("http://[fc00::1]/", "private"),
            ("http://[::]/", "unspecified"),
        ],
    )
    def test_non_public_ipv6_is_rejected(self, url: str, expected: str) -> None:
        assert_blocked(url, because=expected)

    def test_ipv4_mapped_ipv6_does_not_bypass_the_ipv4_checks(self) -> None:
        """``::ffff:127.0.0.1`` is loopback wearing an IPv6 costume. Evaluated
        as an IPv6 address it looks unremarkable, so it must be unwrapped."""
        assert_blocked("http://[::ffff:127.0.0.1]/", because="loopback")

    def test_ipv4_mapped_metadata_address_is_still_blocked(self) -> None:
        with pytest.raises(URLValidationError, match="metadata"):
            _check_address("::ffff:169.254.169.254", url="http://x/")

    def test_the_block_reason_is_specific(self) -> None:
        """is_private is also true for loopback, so a naive check order would
        report every blocked address as merely "private" and lose the detail
        that explains it in the trace."""
        assert "loopback" in assert_blocked("http://127.0.0.1/").reason


class TestHostnameHandling:
    def test_localhost_by_name(self) -> None:
        assert_blocked("http://localhost/internal", because="blocked hostname")

    def test_trailing_dot_does_not_bypass_the_name_check(self) -> None:
        """``localhost.`` is a fully-qualified spelling of the same name."""
        assert_blocked("http://localhost./internal", because="blocked hostname")

    def test_uppercase_does_not_bypass_the_name_check(self) -> None:
        assert_blocked("http://LOCALHOST/internal", because="blocked hostname")

    def test_a_name_resolving_to_loopback_is_rejected(self) -> None:
        """The decision is made on resolved addresses, not on how the name
        looks. A public-looking name can point anywhere."""
        assert_blocked("http://127.0.0.1.nip.io/", because="loopback")

    def test_missing_hostname(self) -> None:
        assert_blocked("http:///path", because="hostname")

    def test_unresolvable_hostname(self) -> None:
        assert_blocked(
            "http://this-domain-should-not-exist-deeptrace-test.invalid/",
            because="resolve",
        )


class TestCredentialsInURL:
    def test_userinfo_is_refused(self) -> None:
        """Leaks secrets into logs and can authenticate to internal services
        on the caller's behalf."""
        assert_blocked("http://user:hunter2@example.com/", because="credentials")

    def test_username_alone_is_refused(self) -> None:
        assert_blocked("http://admin@example.com/", because="credentials")

    def test_credentials_before_an_at_sign_cannot_disguise_the_host(self) -> None:
        """A classic confusion trick: the real host is what follows the @."""
        assert_blocked("http://example.com@127.0.0.1/", because="credentials")


class TestPortRestriction:
    @pytest.mark.parametrize("port", [22, 23, 25, 3306, 5432, 6379, 9200, 11211, 27017])
    def test_service_ports_are_refused(self, port: int) -> None:
        """Without this the fetcher is a port scanner and a protocol-smuggling
        channel against internal services."""
        assert_blocked(f"http://example.com:{port}/", because="port")

    def test_allowlist_not_blocklist(self) -> None:
        """A blocklist has to anticipate every service worth attacking; the
        dangerous ones are the ones nobody thought of."""
        assert {80, 443, 8080, 8443} == ALLOWED_PORTS

    def test_default_ports_are_inferred(self) -> None:
        assert validate_url("https://example.com/").port == 443
        assert validate_url("http://example.com/").port == 80


class TestLegitimateURLs:
    @pytest.mark.parametrize(
        "url",
        [
            "https://kafka.apache.org/documentation/",
            "https://www.rabbitmq.com/tutorials",
            "http://example.com:8080/docs",
        ],
    )
    def test_public_documentation_urls_pass(self, url: str) -> None:
        """A guard that blocks everything is not a working guard."""
        assert validate_url(url).hostname

    def test_resolved_addresses_are_returned(self) -> None:
        validated = validate_url("https://example.com/")
        assert validated.addresses
        assert validated.is_https is True

    def test_is_safe_url_does_not_raise(self) -> None:
        assert is_safe_url("https://example.com/") is True
        assert is_safe_url("http://127.0.0.1/") is False


class TestErrorReporting:
    def test_error_carries_the_url_and_a_reason(self) -> None:
        """A blocked fetch must be explainable in the trace, so a gap in the
        evidence has a recorded cause rather than being unaccounted for."""
        error = assert_blocked("http://169.254.169.254/")

        assert error.url == "http://169.254.169.254/"
        assert error.reason
        assert "169.254.169.254" in str(error)

    def test_empty_url(self) -> None:
        assert_blocked("", because="empty")


class TestTestingEscapeHatch:
    def test_allow_private_permits_loopback(self) -> None:
        """Needed to test the fetcher against a local server. It must never be
        used for URLs derived from retrieved content."""
        assert validate_url("http://127.0.0.1:8080/", allow_private=True).hostname == "127.0.0.1"

    def test_allow_private_still_enforces_scheme_and_port(self) -> None:
        """The escape hatch relaxes address checks only. If it disabled every
        check it would be a bypass rather than a test affordance."""
        with pytest.raises(URLValidationError, match="scheme"):
            validate_url("file:///etc/passwd", allow_private=True)
        with pytest.raises(URLValidationError, match="port"):
            validate_url("http://127.0.0.1:22/", allow_private=True)
