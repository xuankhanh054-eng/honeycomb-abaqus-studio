# -*- coding: utf-8 -*-
"""Abaqus Python 2.7: read the added step without altering either native ODB."""
from __future__ import print_function

import json
import os
import sys

from odbAccess import openOdb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from damage_metrics_fast_98 import RearMetrics

odb_path, output_path, wanted = sys.argv[-3:]
odb = openOdb(path=odb_path, readOnly=True)
try:
    if wanted not in odb.steps.keys():
        raise ValueError('Requested native continuation step missing')
    step = odb.steps[wanted]
    if abs(step.totalTime - 0.008) > 1e-7 or abs(step.timePeriod - 0.004) > 1e-7:
        raise ValueError('Native step does not cover total time 8 to 12 ms')
    histories = []
    for region_name, region in step.historyRegions.items():
        for variable, series in region.historyOutputs.items():
            histories.append({'step': wanted, 'region': region_name,
                              'variable': variable, 'description': series.description,
                              'data': list(series.data)})
    rear = RearMetrics(odb)
    # U is total displacement from the original undeformed reference, also on restart.
    rear.initial = dict((node.label, 0.0) for node in rear.rear.nodes)
    for frame in step.frames:
        rear.frame(frame, frame.frameValue)
    result = {'histories': histories, 'rear': rear.rows, 'damage': rear.summary(),
              'odb_version': str(odb.jobData.version), 'step_name': wanted,
              'step_start_s': step.totalTime, 'step_period_s': step.timePeriod,
              'last_frame_s': step.frames[-1].frameValue, 'frames': len(step.frames),
              'scope': 'Native histories and all rear STATUS/U frames; read only'}
    with open(output_path, 'w') as stream:
        json.dump(result, stream, allow_nan=False)
finally:
    odb.close()
