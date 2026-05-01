"""Internal services — LLM loop, metrics, sentence chunking, hooks, IVR, …

A handful of submodules previously lived under ``getpatter.services`` and
were moved during the v0.5.x refactor:

* Audio primitives → :mod:`getpatter.audio` (``transcoding``, ``pcm_mixer``,
  ``background_audio``).
* Tool primitives → :mod:`getpatter.tools` (``tool_decorator``,
  ``tool_executor``).

For backward compatibility the old ``getpatter.services.<name>`` import
paths still resolve — they alias the new location in :data:`sys.modules`.
New code should import from the canonical module directly.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

# Map of old submodule name → new dotted path. Lazy ``importlib.import_module``
# preserves the existing import semantics (``from getpatter.audio.transcoding
# import mulaw_to_pcm16`` keeps working) without eagerly loading optional
# native deps (numpy/soundfile) at package import time.
_RELOCATED: dict[str, str] = {
    "transcoding": "getpatter.audio.transcoding",
    "pcm_mixer": "getpatter.audio.pcm_mixer",
    "background_audio": "getpatter.audio.background_audio",
    "tool_decorator": "getpatter.tools.tool_decorator",
    "tool_executor": "getpatter.tools.tool_executor",
}


def __getattr__(name: str) -> ModuleType:
    """Resolve ``getpatter.services.<old_name>`` attribute access lazily."""
    target = _RELOCATED.get(name)
    if target is None:
        raise AttributeError(f"module 'getpatter.services' has no attribute {name!r}")
    module = importlib.import_module(target)
    # Cache as both the new dotted path AND the legacy
    # ``getpatter.services.<old_name>`` path so subsequent
    # ``from getpatter.services.<old_name> import X`` statements resolve via
    # the standard import system.
    sys.modules[f"{__name__}.{name}"] = module
    return module


# Eagerly install the legacy aliases in ``sys.modules`` so that
# ``from getpatter.audio.transcoding import …`` works the first time it is
# encountered (Python's import machinery looks up submodules in ``sys.modules``
# before invoking the parent package's ``__getattr__``). Each alias triggers a
# one-time import of the relocated module.
for _old, _new in _RELOCATED.items():
    try:
        _module = importlib.import_module(_new)
    except ImportError:
        # Optional native deps may be missing for some submodules (numpy /
        # soundfile for ``audio.background_audio``). Skip silently — the
        # ``__getattr__`` fallback above will raise the original ImportError
        # at first access if a caller actually depends on the missing module.
        continue
    sys.modules[f"{__name__}.{_old}"] = _module
del _old, _new
