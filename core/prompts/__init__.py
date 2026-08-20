"""Versioned prompts, treated as software components.

Every prompt carries a version (``planner.v1``) that is recorded with each agent
run. This is what makes prompt regression testing and run reproducibility
possible after a prompt changes.
"""
