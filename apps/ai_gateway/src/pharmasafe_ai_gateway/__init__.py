"""Internal AI gateway: the single path to LLM providers.

Owns redaction checks, model routing, budgets, request logging and provider fallback
(Phase 5). No other service may call an LLM provider directly.
"""
