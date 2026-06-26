"""Agents package.

Each flow is a sub-package with its own ReAct agent, prompt and state.
The router is the only module that knows about all of them — agents
themselves never import each other.
"""
