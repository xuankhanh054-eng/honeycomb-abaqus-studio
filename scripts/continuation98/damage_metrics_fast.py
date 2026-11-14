# -*- coding: utf-8 -*-
"""Streaming rear-face metrics from original ODB frames; Python 2.7 compatible."""
import math
import numpy as np


def value_data(value):
    try:
        return value.dataDouble
    except Exception:
        return value.data



def fast_u_values(field, region):
    try:
        groups = [(np.asarray(b.nodeLabels), np.asarray(b.data).reshape((-1, 3)))
                  for b in field.getSubset(region=region).bulkDataBlocks]
        return [(int(label), float(vector[2])) for labels, data in groups
                for label, vector in zip(labels, data)]
    except Exception:
        return [(v.nodeLabel, float(value_data(v)[2])) for v in field.getSubset(region=region).values]


def fast_deleted_labels(field, region):
    try:
        groups = [(np.asarray(b.elementLabels), np.asarray(b.data).reshape(-1))
                  for b in field.getSubset(region=region).bulkDataBlocks]
        return set(int(label) for labels, data in groups for label in labels[data < .5])
    except Exception:
        return set(v.elementLabel for v in field.getSubset(region=region).values
                   if float(value_data(v)) < .5)


class RearMetrics(object):
    def __init__(self, odb):
        self.odb_instances = dict((k.upper(), v) for k, v in odb.rootAssembly.instances.items())
        self.rear = next(v for k, v in odb.rootAssembly.instances.items() if k.upper() == 'BOTTOM-1')
        self.elements = dict((e.label, tuple(e.connectivity)) for e in self.rear.elements)
        self.clamps = set()
        for name, group in self.rear.nodeSets.items():
            if 'CLAMP' in name.upper():
                self.clamps.update(n.label for n in group.nodes)
        self.complete = bool(self.clamps)
        self.initial = {}
        self.rows = []
        self.deleted = set()
        self.connected = set(n.label for n in self.rear.nodes)
        self.max_raw = 0.
        self.max_corrected = 0.
        self.deletion_counts = {}

    def frame(self, frame, total_time):
        outputs = frame.fieldOutputs
        names = outputs.keys()
        if any(name not in names for name in ('PEEQ', 'SDEG', 'JCCRT', 'TEMP')):
            self.complete = False
        if 'U' not in names:
            self.complete = False
            return
        # Repository membership can raise for missing fields; query its key list.
        status = outputs['STATUS'] if 'STATUS' in names else None
        if status is None:
            self.complete = False
            return
        deleted = fast_deleted_labels(status, self.rear)
        if deleted != self.deleted:
            adjacency = {}
            for label, nodes in self.elements.items():
                if label in deleted:
                    continue
                for node in nodes:
                    adjacency.setdefault(node, set()).update(nodes)
            visited = set(self.clamps.intersection(adjacency))
            pending = list(visited)
            while pending:
                for neighbour in adjacency[pending.pop()]:
                    if neighbour not in visited:
                        visited.add(neighbour)
                        pending.append(neighbour)
            self.connected = visited
            self.deleted = deleted
        maximum = 0.
        for node_label, z in fast_u_values(outputs['U'], self.rear):
            if math.isnan(z) or math.isinf(z):
                self.complete = False
                continue
            self.initial.setdefault(node_label, z)
            self.max_raw = max(self.max_raw, -z)
            if node_label in self.connected:
                maximum = max(maximum, self.initial[node_label] - z)
        self.max_corrected = max(self.max_corrected, maximum)
        self.rows.append({'time_s': total_time, 'rear_downward_mm': maximum,
                          'rear_deleted_elements': len(deleted)})
        for instance in ('CORE-1', 'TOP-1', 'BOTTOM-1'):
            subset = status.getSubset(region=self.odb_instances[instance])
            try:
                blocks = list(subset.bulkDataBlocks)
                count = sum(len(block.data) for block in blocks)
                deleted_count = sum(int(np.count_nonzero(np.asarray(block.data) < .5)) for block in blocks)
            except Exception:
                values = subset.values
                count = len(values)
                deleted_count = sum(float(value_data(v)) < .5 for v in values)
            if not count:
                self.complete = False
            self.deletion_counts[instance] = max(self.deletion_counts.get(instance, 0), deleted_count)

    def summary(self):
        return {'damage_fields_complete': self.complete and bool(self.rows),
                'max_rear_downward_mm': self.max_corrected,
                'max_rear_raw_downward_mm': self.max_raw,
                'rear_end_downward_mm': self.rows[-1]['rear_downward_mm'] if self.rows else None,
                'rear_skin_deleted': bool(self.deletion_counts.get('BOTTOM-1', 0)),
                'deleted_elements_by_instance': self.deletion_counts,
                'rear_measurement': 'max over time and nodes connected to clamp; U3 minus initial frame',
                'rear_frame_count': len(self.rows)}
