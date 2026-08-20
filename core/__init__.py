"""DeepTrace core: agents, orchestration, tools, prompts, and evaluation.

This package holds everything that is independent of how DeepTrace is served.
Nothing here imports from ``apps``. That direction is deliberate -- the research
engine must be runnable from a script, a worker, or a test without a web server.
"""
