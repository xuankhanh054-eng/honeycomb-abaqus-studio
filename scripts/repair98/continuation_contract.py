"""Pure checks for extending the same native Explicit model, never fresh loading."""

import math
import re

START = 0.008
EXTENSION = 0.004
TARGET = 0.012
STEP = "ImpactContinue_8to12ms"


def restart_deck(source):
    source = source.replace("\r\n", "\n")
    blocks = re.findall(r"(?ims)^\*Step\b[^\n]*\n.*?^\*End Step[^\n]*(?:\n|$)", source)
    if len(blocks) != 1:
        raise ValueError("Expected exactly one original impact step")
    step = blocks[0]
    if re.search(
        r"(?im)^\*(?:Initial Conditions|Velocity|Mass Scaling|Fixed Mass Scaling|Variable Mass Scaling)\b",
        step,
    ):
        raise ValueError("Restart step must not reset initial conditions or add mass scaling")
    step, count = re.subn(r"(?im)^(\*Step,\s*name=)[^,\n]+", r"\g<1>" + STEP, step)
    if count != 1:
        raise ValueError("Unknown original step identity")
    step, count = re.subn(
        r"(?im)^(\*Dynamic, Explicit, ADIABATIC\s*\n)\s*,\s*0\.008\s*$",
        r"\g<1>, 0.004",
        step,
    )
    if count != 1:
        raise ValueError("Original adiabatic 8 ms step not found")
    # Preserve 10 us field spacing, 2 us history spacing and restart spacing.
    step, count = re.subn(r"(?im)^(\*Output, field, number interval=)800\s*$", r"\g<1>400", step)
    if count != 1 or not re.search(r"(?im)^\*Output, history, time interval=2e-06\s*$", step):
        raise ValueError("Unknown original output sampling")
    step = re.sub(r"(?im)^(\*Restart, write, number interval=)48(?=,)", r"\g<1>24", step)
    return "*Heading\nVerified continuation from 8 ms to total 12 ms\n*Restart, read, step=1\n" + step


def compare_boundary(reference, continuation, *, atol=1e-4):
    """Raw native displacement/velocity are mm and mm/s; energy columns are J."""
    fields = ["impactor_displacement_mm", "velocity_mm_s", "CORE_ALLPD_J", "CORE_ALLDMD_J"]
    errors = {}
    for key in fields:
        before, after = float(reference[key]), float(continuation[key])
        if not math.isfinite(before) or not math.isfinite(after):
            raise ValueError("Missing or nonfinite boundary state: " + key)
        error = abs(before - after)
        errors[key] = error
        if error > atol + 1e-6 * max(abs(before), abs(after)):
            raise ValueError("Restart boundary differs: " + key)
    return errors


def merge_rows(original, segment):
    if not original or not segment:
        raise ValueError("Cannot merge empty histories")
    if abs(float(original[-1]["time_s"]) - START) > 1e-8:
        raise ValueError("Original history does not end at 8 ms")
    if abs(float(segment[0]["time_s"])) > 3e-6 or abs(float(segment[-1]["time_s"]) - EXTENSION) > 3e-6:
        raise ValueError("Continuation does not cover all 4 ms")
    boundary = compare_boundary(original[-1], segment[0])
    combined = [dict(row) for row in original]
    for row in segment:
        time = START + float(row["time_s"])
        if time <= float(original[-1]["time_s"]) + 1e-8:
            continue
        combined.append({**row, "time_s": time, "time_ms": time * 1000})
    times = [float(row["time_s"]) for row in combined]
    if abs(times[-1] - TARGET) > 3e-6 or any(
        not 0 < b - a <= 3e-6 for a, b in zip(times[:-1], times[1:], strict=True)
    ):
        raise ValueError("Missing, duplicate or nonincreasing original/continuation samples")
    return combined, boundary
