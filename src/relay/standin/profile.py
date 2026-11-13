"""Behaviour parameters of the stand-in model server.

PROVENANCE: every number here is CHOSEN by the author (label SIMULATED). None
is measured from Groq. Calibration against real probes is a later phase; until
then the load results say "against a stand-in with these parameters", nothing more.

The stand-in's rate limits default to the documented account limits (8K TPM,
30 RPM, 1000 RPD, 200K TPD) and a scenario may scale them (`limits_scale`) to get
a usable sample size in minutes. The scale used is written to the run manifest.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Mapping


@dataclass
class StandinProfile:
    seed: int = 1
    # Latency (SIMULATED).
    ttft_median_ms: float = 400.0      # time to first content token, lognormal median
    ttft_sigma: float = 0.35           # lognormal sigma
    tokens_per_s: float = 150.0        # decode speed at no load
    stream_chunk_words: int = 3        # words per SSE event
    knee_inflight: int = 4             # concurrent requests before slowdown begins
    slowdown_alpha: float = 0.15       # latency x (1 + alpha * max(0, inflight - knee))
    # Rate limits the stand-in enforces. 0 means "no limit of this kind".
    rpm: int = 30
    tpm: int = 8000
    rpd: int = 1000
    tpd: int = 200000
    daily_retry_after_s: float = 21600.0   # SIMULATED (guess): what a daily 429 advertises
    # Failure injection (CHOSEN probabilities, independent coin flips).
    p_5xx: float = 0.02                # answered with 503 after a short delay
    p_hang: float = 0.005              # accepted, then no response for hang_s
    p_slow: float = 0.03               # succeeds, but slow_factor times slower
    slow_factor: float = 8.0
    p_midstream: float = 0.01          # connection dropped part-way through the stream
    p_bad_citation: float = 0.02       # reply cites a source that was not provided
    hang_s: float = 60.0
    # Content.
    reply_words: int = 45              # words quoted from the first provided source

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "StandinProfile":
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown stand-in profile keys: {sorted(unknown)}")
        return cls(**dict(d))

    def update(self, d: Mapping[str, Any]) -> None:
        known = {f.name for f in fields(self)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown stand-in profile keys: {sorted(unknown)}")
        for k, v in d.items():
            setattr(self, k, type(getattr(self, k))(v))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def scaled_limits(self, scale: float) -> "StandinProfile":
        """A copy with rpm/tpm/rpd/tpd multiplied by `scale` (0 stays 0)."""
        d = self.to_dict()
        for k in ("rpm", "tpm", "rpd", "tpd"):
            d[k] = int(round(d[k] * scale))
        return StandinProfile(**d)
