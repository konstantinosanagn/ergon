"""Deterministic feature transforms for the sector classifier.

Pure and dependency-light: numpy is imported lazily inside the vector-math helpers (the repo
pattern, see ``index/rich.py``), so importing this module costs nothing at runtime and adds no hard
dependency. Shared by the offline trainer (``scripts/train_sector_classifier.py``) and the numpy-only
runtime inference (``sector_clf.py``) — identical features on both sides, by construction.
"""

from __future__ import annotations

import re

# TLD group -> the suffixes that map to it. Small, high-signal industry priors on the domain TLD.
SECTOR_TLD_GROUPS: dict[str, tuple[str, ...]] = {
    "tech": (".ai", ".io", ".dev", ".app", ".tech"),
    "finance": (".bank", ".finance", ".insurance"),
    "education": (".edu", ".ac.uk", ".edu.au"),
    "government": (".gov", ".mil", ".gov.uk"),
    "health": (".health", ".care"),
    "media": (".tv", ".fm", ".news"),
}
# Stable, sorted group order — the feature layout MUST NOT drift (the .npz depends on it).
TLD_VOCAB: tuple[str, ...] = tuple(sorted(SECTOR_TLD_GROUPS))

_WS = re.compile(r"\s+")


def _registrable_label(domain: str | None) -> str:
    """The domain's second-level label, TLD stripped ('careers.acme-bank.com' -> 'acme-bank')."""
    if not domain:
        return ""
    host = domain.strip().lower().split("/")[0]
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 2:
        # drop known multi-part public suffixes' last two labels, else the last one
        return (
            parts[-3]
            if parts[-2] in {"co", "com", "ac", "gov", "edu"} and len(parts) >= 3
            else parts[-2]
        )
    return parts[0] if parts else ""


def build_input_text(name: str | None, domain: str | None, title: str | None) -> str:
    """The string fed to the embedder: '{name}. {registrable-domain-label}. {example title}'."""
    parts = [p for p in (name, _registrable_label(domain), title) if p]
    return _WS.sub(" ", ". ".join(s.strip() for s in parts)).strip()


def tld_features(domain: str | None) -> list[float]:
    """Fixed-width one-hot over ``TLD_VOCAB`` (all-zero when no group matches)."""
    vec = [0.0] * len(TLD_VOCAB)
    if not domain:
        return vec
    host = domain.strip().lower().split("/")[0]
    for i, group in enumerate(TLD_VOCAB):
        if any(host.endswith(suf) for suf in SECTOR_TLD_GROUPS[group]):
            vec[i] = 1.0
            break  # one group max — keeps the feature a clean indicator
    return vec
