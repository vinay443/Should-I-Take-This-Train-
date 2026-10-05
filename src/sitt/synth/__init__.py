"""Synthetic (invented) observations, for building the pipeline before real data exists."""

from sitt.synth.generate import (
    SOURCE,
    BuildResult,
    SynthConfig,
    SynthData,
    SynthError,
    build_database,
    simulate,
    write_batches,
)

__all__ = [
    "SOURCE",
    "BuildResult",
    "SynthConfig",
    "SynthData",
    "SynthError",
    "build_database",
    "simulate",
    "write_batches",
]
