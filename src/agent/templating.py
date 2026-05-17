from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, StrictUndefined


@lru_cache(maxsize=1)
def _env() -> Environment:
    return Environment(
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=False,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )


def render_string(source: str, **vars) -> str:
    return _env().from_string(source).render(**vars)


def render_file(path: Path, **vars) -> str:
    return render_string(path.read_text(encoding="utf-8"), **vars)


__all__ = ["render_string", "render_file"]
