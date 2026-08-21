"""SSRF protection for outbound URL fetching.

DeepTrace fetches URLs that come from search results and, indirectly, from the
content of pages it has already read. That makes the fetcher an attacker-
influenced HTTP client running inside the trust boundary, which is the exact
shape of a Server-Side Request Forgery vulnerability.

The classic exploit: a page or search result points at
``http://169.254.169.254/latest/meta-data/iam/security-credentials/`` and the
research agent politely fetches the deployment's cloud credentials and writes
them into evidence.

Every check here exists because skipping it enables a specific attack:

``http``/``https`` only
    ``file://`` reads local disk. ``gopher://`` and ``dict://`` can be used to
    speak other protocols and reach internal services that trust the network.

No credentials in the URL
    ``http://user:pass@internal/`` both leaks secrets into logs and can
    authenticate to internal services on the caller's behalf.

Port allowlist
    Blocks the fetcher being used as a scanner or protocol-smuggling channel
    against SSH, SMTP, Redis, PostgreSQL, and similar.

Every resolved IP must be public
    A hostname is not a destination. ``localhost.attacker.com`` can resolve to
    ``127.0.0.1``, so the decision must be made on resolved addresses, and on
    *all* of them -- a host with one public and one private record must be
    rejected, not sampled.

Metadata endpoints blocked by name and address
    Cloud metadata services are the highest-value SSRF target and are reachable
    through several aliases.

Redirects revalidated at every hop
    Validating only the first URL is the most common way SSRF protection is
    defeated: a public URL returns ``302`` to ``127.0.0.1``.

Response size and timeout limits
    An endless or enormous response is a denial-of-service against the worker.

Known residual risk: DNS rebinding
-----------------------------------
Validation resolves the hostname, then the HTTP client resolves it again when
connecting. A hostile DNS server can answer differently the second time, so a
name that validated as public can connect to a private address. Closing this
requires pinning the connection to the validated IP address, which needs a
custom transport that overrides TLS SNI. That is deliberately not implemented
here, and it is recorded rather than left implicit, because an undocumented gap
is indistinguishable from an unnoticed one.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})

ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})
"""Ports a documentation or article site plausibly serves on.

An allowlist rather than a blocklist of dangerous ports: a blocklist has to
anticipate every service worth attacking, and the interesting ones are the ones
nobody thought of.
"""

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
"""Cap on a fetched page. Generous for an article, far below what would exhaust
a worker's memory."""

MAX_REDIRECTS = 5

BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
    }
)
"""Names that resolve to infrastructure endpoints.

Redundant with the IP checks in a correctly configured environment, and kept
because defence in depth is the point: a split-horizon or misconfigured resolver
can make these resolve to something that passes an address check.
"""

BLOCKED_ADDRESSES = frozenset(
    {
        "169.254.169.254",  # AWS, Azure, GCP, DigitalOcean instance metadata
        "fd00:ec2::254",  # AWS IMDS over IPv6
        "100.100.100.200",  # Alibaba Cloud metadata
        "192.0.0.192",  # Oracle Cloud metadata
    }
)


class URLValidationError(ValueError):
    """A URL was rejected before any request was made.

    Carries the URL and a specific reason so a blocked fetch is explainable in
    the research trace rather than appearing as an unexplained gap in evidence.
    """

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"Refused to fetch {url!r}: {reason}")
        self.url = url
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ValidatedURL:
    """A URL that passed every check, with the addresses it resolved to."""

    url: str
    scheme: str
    hostname: str
    port: int
    addresses: tuple[str, ...]

    @property
    def is_https(self) -> bool:
        return self.scheme == "https"


def _resolve(hostname: str) -> tuple[str, ...]:
    """Resolve a hostname to every address it maps to.

    All records are returned rather than the first, because the decision must
    consider every address the client might connect to. A host answering with
    one public and one private address is not safe merely because the public one
    was checked first.
    """
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise URLValidationError(hostname, f"hostname does not resolve ({exc})") from exc

    addresses = {str(info[4][0]) for info in infos}
    if not addresses:
        raise URLValidationError(hostname, "hostname resolved to no addresses")
    return tuple(sorted(addresses))


def _check_address(address: str, *, url: str) -> None:
    """Reject any address that is not publicly routable."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as exc:
        raise URLValidationError(url, f"unparseable address {address!r}") from exc

    if address in BLOCKED_ADDRESSES:
        raise URLValidationError(url, f"{address} is a cloud metadata endpoint")

    # Unwrap IPv4-mapped IPv6 (::ffff:127.0.0.1), which would otherwise bypass
    # the IPv4 private-range checks by being evaluated as an IPv6 address.
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
        if str(parsed) in BLOCKED_ADDRESSES:
            raise URLValidationError(url, f"{parsed} is a cloud metadata endpoint")

    # Ordered most specific first. ip_address.is_private is also True for
    # loopback and link-local addresses, so checking it first would report every
    # blocked address as merely "private" and lose the detail that explains the
    # block in the research trace.
    disqualifiers = (
        (parsed.is_unspecified, "an unspecified address"),
        (parsed.is_loopback, "a loopback address"),
        (parsed.is_link_local, "a link-local address"),
        (parsed.is_multicast, "a multicast address"),
        (parsed.is_reserved, "a reserved address"),
        (parsed.is_private, "a private address"),
    )
    for failed, description in disqualifiers:
        if failed:
            raise URLValidationError(url, f"{parsed} is {description}")

    if not parsed.is_global:
        raise URLValidationError(url, f"{parsed} is not publicly routable")


def validate_url(url: str, *, allow_private: bool = False) -> ValidatedURL:
    """Validate a URL for outbound fetching.

    Args:
        url: The candidate URL, from a search result or page content.
        allow_private: Permit private and loopback addresses. For tests against
            a local server only. Never enable it for URLs derived from
            retrieved content, which is the entire threat model.

    Raises:
        URLValidationError: With a specific reason, so the trace can record why
            a source was not fetched.
    """
    candidate = url.strip()
    if not candidate:
        raise URLValidationError(url, "empty URL")

    parts = urlsplit(candidate)

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise URLValidationError(
            url,
            f"scheme {parts.scheme!r} is not allowed "
            f"(permitted: {', '.join(sorted(ALLOWED_SCHEMES))})",
        )

    if parts.username or parts.password:
        raise URLValidationError(url, "URLs carrying credentials are not fetched")

    hostname = parts.hostname
    if not hostname:
        raise URLValidationError(url, "no hostname")

    hostname = hostname.rstrip(".").lower()
    if hostname in BLOCKED_HOSTNAMES:
        raise URLValidationError(url, f"{hostname!r} is a blocked hostname")

    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise URLValidationError(url, "invalid port") from exc

    if port not in ALLOWED_PORTS:
        raise URLValidationError(
            url,
            f"port {port} is not allowed (permitted: "
            f"{', '.join(str(p) for p in sorted(ALLOWED_PORTS))})",
        )

    addresses = _resolve(hostname)
    if not allow_private:
        for address in addresses:
            _check_address(address, url=url)

    return ValidatedURL(
        url=candidate,
        scheme=parts.scheme.lower(),
        hostname=hostname,
        port=port,
        addresses=addresses,
    )


def is_safe_url(url: str) -> bool:
    """Whether a URL would pass validation. For filtering search results."""
    try:
        validate_url(url)
    except URLValidationError:
        return False
    return True
