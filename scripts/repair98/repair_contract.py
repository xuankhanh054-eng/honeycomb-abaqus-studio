"""Recognize native timed checkpoints without masking numerical failures."""

import math
import re


def orderly_stop(log, acknowledgment, *, requested):
    if not requested or not acknowledgment:
        raise ValueError("No requested and acknowledged timed stop")
    step_time = acknowledgment.get("simulation_time_s")
    increment = acknowledgment.get("increment")
    if not isinstance(step_time, (int, float)) or not math.isfinite(step_time) or not 0 < step_time < 0.004:
        raise ValueError("Invalid native continuation checkpoint time")
    if type(increment) is not int or increment <= 0:
        raise ValueError("Invalid native checkpoint increment")
    if "exit as per user request in VEXTERNALDB" not in log:
        raise ValueError("Native log does not identify requested VEXTERNALDB shutdown")
    errors = re.findall(r"(?im)^\*\*\*ERROR:\s*([^\n]+)", log)
    allowed = (
        "The program will exit as per user request in VEXTERNALDB.",
        "The fatal error will generate a new field output in the last",
    )
    if any(not any(error.strip().startswith(prefix) for prefix in allowed) for error in errors):
        raise ValueError("Independent native numerical error; not a safe timed checkpoint")
    if not re.search(r"Restart Number\s+\d+\s+at\s+[0-9.Ee+-]+", log):
        raise ValueError("No native restart-write confirmation")
    return {
        "timed_stop_confirmed": True,
        "step_time_s": float(step_time),
        "total_time_s": 0.008 + float(step_time),
        "increment": increment,
        "nonzero_exit_expected_for_vexternaldb_termination": True,
    }
