"""Repositories: the only place SQL lives.

Agents produce domain objects; repositories translate those into rows. That
boundary is what lets the entire research engine be tested without PostgreSQL
running, and keeps query code out of agent modules where it would be both
untestable and easy to get subtly wrong.
"""
