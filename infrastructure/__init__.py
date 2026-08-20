"""Adapters for external infrastructure: database, cache, and queue.

Isolating these keeps storage and transport decisions out of the research
engine, so ``core`` can be tested without PostgreSQL or Redis running.
"""
