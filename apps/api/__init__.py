"""FastAPI service: HTTP routes, WebSocket progress, auth, and job submission.

The API never runs a research workflow inline. It validates, persists, enqueues,
and returns immediately -- research is long-running and belongs in the worker.
"""
