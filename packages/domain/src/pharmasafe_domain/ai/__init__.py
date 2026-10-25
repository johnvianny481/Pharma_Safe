"""Deterministic rules around the AI pipeline (Phase 5).

Everything here is pure and model-free: redaction, evidence checking, the rule-based
seriousness signal, duplicate blocking and similarity, automation levels and follow-up
gaps. The AI worker combines these with model output; the model never decides alone.
"""
