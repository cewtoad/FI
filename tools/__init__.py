"""Developer tools (recording, replay, probes).

Not imported by the app at runtime; safe to exclude from the distribution
(see build manifest). Provided as a package so tests can do
``from tools.replay import replay_into``.
"""
