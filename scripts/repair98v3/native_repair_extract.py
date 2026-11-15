# -*- coding: utf-8 -*-
"""Abaqus Python 2.7 read-only checkpoint audit and native history extraction."""
from __future__ import print_function

import json
import os
import sys

from odbAccess import openOdb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from damage_metrics_fast_98 import RearMetrics

mode, odb_path, output_path, step_name = sys.argv[-4:]
odb = openOdb(path=odb_path, readOnly=True)
try:
    step = odb.steps[step_name]
    histories = []
    for region_name, region in step.historyRegions.items():
        for variable, series in region.historyOutputs.items():
            histories.append({'step': step_name, 'region': region_name, 'variable': variable,
                              'description': series.description, 'data': list(series.data)})
    result = {'histories': histories, 'step_name': step_name, 'step_start_s': step.totalTime,
              'step_period_s': step.timePeriod, 'last_frame_s': step.frames[-1].frameValue,
              'frames': len(step.frames), 'odb_version': str(odb.jobData.version),
              'steps_available': list(odb.steps.keys()), 'read_only': True}
    if mode == 'full':
        rear = RearMetrics(odb)
        rear.initial = dict((node.label, 0.0) for node in rear.rear.nodes)
        for frame in step.frames:
            rear.frame(frame, frame.frameValue)
        result.update(rear=rear.rows, damage=rear.summary())
    with open(output_path, 'w') as stream:
        json.dump(result, stream, allow_nan=False)
finally:
    odb.close()
