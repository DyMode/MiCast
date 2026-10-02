"""Common source fan-out shared by classic, AirPlay 2 and media ingress."""

from dataclasses import dataclass

from micast.pcm_tee import PCMTee


@dataclass
class PipelineBranches:
    readers: list
    tap: object = None
    tee: PCMTee | None = None


def build_branches(reader, variants, owner, sample_rate, wants_tap=False, start=True):
    if len(variants) == 1 and not wants_tap:
        return PipelineBranches([reader])
    tee = PCMTee(reader, len(variants) + int(wants_tap), sample_rate=sample_rate)
    for output, variant in zip(tee.outputs, variants, strict=False):
        output.name = owner + variant["suffix"]
    if start:
        tee.start()
    return PipelineBranches(
        tee.outputs[: len(variants)], tee.outputs[-1] if wants_tap else None, tee
    )
