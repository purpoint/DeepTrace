"""Deterministic tools around external capabilities.

Tools do not reason and agents do not fetch. A tool takes structured input,
performs one external action, and returns structured output plus a trace event.
Content returned by these tools is untrusted data, never instructions.
"""
