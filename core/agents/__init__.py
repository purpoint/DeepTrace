"""Specialized research agents.

Each agent has exactly one responsibility and its own versioned prompt:

    planner       decomposes a question into atomic research tasks
    researcher    executes a task using tools and collects sources
    evidence      turns raw sources into structured, attributed evidence
    analyst       compares evidence, surfaces agreements and contradictions
    fact_checker  decides whether a claim is actually supported
    writer        composes the final report from verified claims only

The separation is not cosmetic. The fact checker exists precisely so that the
writer is never the component deciding whether its own claims hold up.
"""
