"""Per-flow tool registries.

Each agent imports only its own flow's tools — there is no global
ALL_TOOLS list. Cross-cutting tools live under `tools/shared/` and are
imported individually by the agents that need them.
"""
