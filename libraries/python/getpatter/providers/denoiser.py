"""Bring-your-own-license denoiser registry.

Maps a small set of stable, string model ids to a resolved
:class:`getpatter.providers.base.AudioFilter`. Selection is opt-in via
``Agent.denoiser``; the default (``None``) leaves the inbound audio path
byte-identical.

The Krisp models are **bring-your-own-license**: Patter ships ZERO Krisp
binaries, license keys, or ``.kef`` model files. Resolving a ``krisp-*`` id
constructs the existing :class:`~getpatter.providers.krisp_filter.KrispVivaFilter`
pointing at the operator's own model directory (``KRISP_MODELS_DIR`` + the
model id's ``.kef`` filename) and requires the operator's own ``krisp-audio``
SDK plus a ``KRISP_VIVA_SDK_LICENSE_KEY``. When the SDK, license, or model
file is missing, resolution raises a clear, actionable error naming exactly
what to install or set — it never silently degrades to a no-op filter.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from getpatter.providers.base import AudioFilter
from getpatter.providers.krisp_instance import KrispModelKind

# Environment variable pointing at the directory that holds the operator's
# licensed Krisp ``.kef`` model files (one per registered denoiser id).
KRISP_MODELS_DIR_ENV = "KRISP_MODELS_DIR"


@dataclass(frozen=True)
class DenoiserSpec:
    """Static description of a selectable denoiser model.

    Attributes
    ----------
    model_id:
        Public, stable string id surfaced through ``Agent.denoiser``.
    kind:
        Krisp session family the model loads through (:class:`KrispModelKind`).
    filename:
        BYO ``.kef`` file expected under ``KRISP_MODELS_DIR``.
    description:
        Human-readable one-liner (docs / error context only).
    """

    model_id: str
    kind: KrispModelKind
    filename: str
    description: str


# The two REAL Krisp model ids Patter recognises. Keep this list tight — each
# id must map to a concrete, documented Krisp model the operator can license.
_DENOISER_SPECS: tuple[DenoiserSpec, ...] = (
    DenoiserSpec(
        model_id="krisp-viva-tel-v2",
        kind=KrispModelKind.NC,
        filename="krisp-viva-tel-v2.kef",
        description="Krisp VIVA telephony noise cancellation (NC session).",
    ),
    DenoiserSpec(
        model_id="krisp-bvc-o-pro-v3",
        kind=KrispModelKind.BVC,
        filename="krisp-bvc-o-pro-v3.kef",
        description="Krisp Background Voice Cancellation, pro (BVC session).",
    ),
)

# Public registry: model id -> spec.
DENOISERS: dict[str, DenoiserSpec] = {spec.model_id: spec for spec in _DENOISER_SPECS}
DENOISER_IDS: tuple[str, ...] = tuple(DENOISERS.keys())


def resolve_denoiser(name: str) -> AudioFilter:
    """Resolve a string denoiser id to a concrete :class:`AudioFilter`.

    Parameters
    ----------
    name:
        A registered denoiser id (see :data:`DENOISER_IDS`).

    Returns
    -------
    AudioFilter
        A ready-to-use filter instance for the pipeline inbound chain.

    Raises
    ------
    ValueError
        If ``name`` is not a registered denoiser id.
    RuntimeError
        If the required Krisp model directory (``KRISP_MODELS_DIR``) is unset,
        or the ``krisp-audio`` SDK / license is missing.
    FileNotFoundError
        If the model directory is set but the model ``.kef`` file is absent.
    """
    spec = DENOISERS.get(name)
    if spec is None:
        known = ", ".join(sorted(DENOISERS))
        raise ValueError(f"Unknown denoiser {name!r}. Known denoisers: {known}.")
    return _resolve_krisp(spec)


def _resolve_krisp(spec: DenoiserSpec) -> AudioFilter:
    """Build a :class:`KrispVivaFilter` for a Krisp-family denoiser spec."""
    models_dir = os.getenv(KRISP_MODELS_DIR_ENV)
    if not models_dir:
        raise RuntimeError(
            f"Denoiser {spec.model_id!r} needs the Krisp model directory: set "
            f"{KRISP_MODELS_DIR_ENV} to the folder containing {spec.filename!r}. "
            "Krisp is bring-your-own-license — Patter ships no Krisp SDK, "
            "license, or model files. Install the SDK (pip install "
            "getpatter[krisp]), set KRISP_VIVA_SDK_LICENSE_KEY, and place your "
            "licensed .kef model in that directory."
        )

    model_path = os.path.join(models_dir, spec.filename)

    # Imported lazily so the heavy ``krisp-audio`` import (and its SDK guard)
    # is only paid when a Krisp denoiser is actually selected.
    from getpatter.providers.krisp_filter import KrispVivaFilter

    return KrispVivaFilter(model_path=model_path, session_type=spec.kind)
