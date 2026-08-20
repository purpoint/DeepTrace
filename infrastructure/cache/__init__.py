"""Redis caching for search results, page extractions, and embeddings.

Cache keys are hashes of deterministic inputs. LLM completions are deliberately
not cached by default -- stale synthesis is worse than a repeated call.
"""
