"""Provider-agnostic LLM layer.

Agents depend on the provider interface, never on a vendor SDK. OpenAI is the
first implementation; additional providers are added as adapters without any
change to agent code. This is what allows per-agent model routing across
different providers within a single research run.
"""
