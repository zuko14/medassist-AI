"""Kriya Ketu: the internal staff action assistant (docs/kriya-ketu/).

Imports nothing from the patient channels (app/services/conversation.py,
app/voice/dialog.py); writes only assistant_runs. Domain writes go through the
existing /admin endpoints with the caller's own credentials (ADR-004).
"""
