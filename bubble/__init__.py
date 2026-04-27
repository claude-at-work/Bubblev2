"""bubble — content-addressed package store with thin runtime views.

The vault is the store: artifacts addressed by (name, version, wheel_tag).
Bubbles are the views: ephemeral or long-lived, composed by symlink.

Two consumer surfaces:
  - The CLI (`python3 -m bubble`) for humans at a terminal.
  - `bubble.AgentVault` for agent runtimes embedding bubble as a
    library. See `bubble/agent.py` for the embedding shape.
"""

__version__ = "0.3.0"


def __getattr__(name: str):
    # Lazy export: importing bubble shouldn't pull in agent.py (which
    # imports vault.fetcher → urllib etc.) unless the embedding API is
    # actually used. Keeps `import bubble` cheap for callers that only
    # need __version__.
    if name == "AgentVault":
        from .agent import AgentVault
        return AgentVault
    raise AttributeError(f"module 'bubble' has no attribute {name!r}")
