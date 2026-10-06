"""Kriya AI Receptionist (voice). See docs/voice/voice-architecture.md.

Pure modules (no app.* imports, unit-testable in isolation):
  phone, audio, dates, lexicon, intents, language, nlu_rules, policy,
  responses, dialog, exotel_protocol.
I/O modules: store, tools, nlu_llm, safety, session, providers/*, gateway.
"""
