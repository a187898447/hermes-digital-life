"""Python domain package for the Hermes digital-life plugin."""

# Eager import when the host storage API is importable: callers (tests, CLI) reach
# ``digital_life.storage`` by attribute, so its presence must not depend on import order.
# Outside the host (bare unit tests) the API is absent and we degrade quietly.
try:  # pragma: no cover - the branch is exercised by every test module
    from . import storage  # noqa: E402,F401
except ImportError:  # pragma: no cover
    pass
