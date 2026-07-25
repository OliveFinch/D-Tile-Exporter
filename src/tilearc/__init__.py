"""tilearc - a polite archiver for historical Disney park map tiles."""

__version__ = "0.1.0"

TOOL_NAME = "tilearc"

#: Sent on every request so operators can identify (and if needed, block) us.
USER_AGENT = (
    f"{TOOL_NAME}/{__version__} "
    "(Magic Parks Explorer historical map archiver; "
    "+https://github.com/OliveFinch/D-Tile-Exporter)"
)

__all__ = ["__version__", "TOOL_NAME", "USER_AGENT"]
