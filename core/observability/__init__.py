"""Run recording: the durable record of what the system actually did.

Separate from ``core.logging``. Logs are for humans reading a stream; run
records are structured rows meant to be queried, aggregated, and replayed.
Cost totals, latency percentiles, and the research trace all come from here.
"""
