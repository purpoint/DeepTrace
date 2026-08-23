"""Identity: proving who is asking, and remembering it between requests.

Adapters, like everything else in ``infrastructure``. Argon2 lives here, PyJWT
lives here, and the Redis keys that make a session revocable live here, so that
the layer above deals in "a user id" and never in a hash format or a claim name.

Nothing in ``core`` imports any of it. The research engine has no concept of an
account -- it takes a question and produces a run -- and that is what lets the
same engine be driven by an authenticated API, an unauthenticated CLI, and a
test, without a single conditional about who is asking.
"""
