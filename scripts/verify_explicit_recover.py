"""Licensed one-element stop/recover test. No research rows are published."""
import argparse
import json
import math
import shutil
import subprocess
import time
from pathlib import Path

from native_cloud_worker import native_runtime

from honeycomb_studio import solver
from honeycomb_studio.cloud_continuation import checkpoint_inventory, verify_checkpoint, verify_solver_cleanup
from honeycomb_studio.data import history_continuity
from honeycomb_studio.store import write_json

ROOT = Path('/tmp/honeycomb-recover-probe')
BUILD = '''from abaqus import *
from abaqusConstants import *
import part, material, section, assembly, step, interaction, load, mesh, job
import regionToolset, visualization, os
Mdb()
m=mdb.models['Model-1']
s=m.ConstrainedSketch(name='box',sheetSize=10.)
s.rectangle(point1=(0.,0.),point2=(1.,1.))
p=m.Part(name='Cube',dimensionality=THREE_D,type=DEFORMABLE_BODY)
p.BaseSolidExtrude(sketch=s,depth=1.)
a=m.rootAssembly
i=a.Instance(name='Cube-1',part=p,dependent=ON)
mat=m.Material(name='Elastic')
mat.Density(table=((2.7e-9,),))
mat.Elastic(table=((70000.,.33),))
m.HomogeneousSolidSection(name='S',material='Elastic')
p.SectionAssignment(region=p.Set(name='ALL',cells=p.cells),sectionName='S')
p.seedPart(size=.5)
p.setMeshControls(regions=p.cells,technique=STRUCTURED)
p.setElementType(regions=(p.cells,),elemTypes=(mesh.ElemType(elemCode=C3D8R,elemLibrary=EXPLICIT),))
p.generateMesh()
a.regenerate()
m.ExplicitDynamicsStep(name='Test',previous='Initial',timePeriod=.06)
m.steps['Test'].Restart(numberIntervals=1000,timeMarks=OFF,overlay=ON)
m.fieldOutputRequests['F-Output-1'].setValues(numIntervals=10)
m.historyOutputRequests['H-Output-1'].setValues(numIntervals=1000)
j=mdb.Job(name='recover_probe',model='Model-1',numCpus=4,numDomains=4,explicitPrecision=DOUBLE,nodalOutputPrecision=FULL)
j.writeInput(consistencyChecking=ON)
'''


def main():
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=('all', 'create', 'restore'), default='all')
    parser.add_argument('--damage', action='store_true', help='Verify JC damage/thermal/moving shell state')
    parser.add_argument('--root', type=Path, default=ROOT, help='Separate probe directory for independent specimens')
    args = parser.parse_args()
    ROOT = args.root.resolve()
    if args.stage != 'restore':
        ROOT.mkdir()
        (ROOT / 'abaqus_v6.env').write_text('verbose=ON\n')
        (ROOT / 'build.py').write_text(BUILD)
        if args.damage:
            from honeycomb_studio.campaign_coupons import write_recovery_coupon
            write_recovery_coupon(ROOT)
    # Exercise the same filename pattern as Engine, including native linking.
    control = ROOT / 'recover_probe_continuation.c'
    shutil.copy2('scripts/linux_media/continuation_control.c', control)
    reader = ROOT / 'read_history.py'
    reader.write_text('''from odbAccess import openOdb
import json, sys
odb=openOdb('recover_probe.odb',readOnly=True)
channels=[]
for step, obj in odb.steps.items():
    for region, hist in obj.historyRegions.items():
        for variable, series in hist.historyOutputs.items():
            channels.append(dict(step=step,region=region,variable=variable,data=list(series.data)))
states=[]
for step, obj in odb.steps.items():
    for frame in obj.frames:
        fields={}
        for name in ('U','V','PEEQ','SDEG','JCCRT','STATUS','TEMP'):
            if name not in frame.fieldOutputs.keys(): continue
            values=[]
            for v in frame.fieldOutputs[name].values:
                try: data=v.dataDouble
                except: data=v.data
                values.append([float(x) for x in data] if hasattr(data,'__len__') else float(data))
            fields[name]=values
        states.append(dict(time=frame.frameValue,fields=fields))
odb.close()
open(sys.argv[-1],'w').write(json.dumps(channels))
open(sys.argv[-1]+'.state.json','w').write(json.dumps(states))
''')
    with native_runtime(ROOT, continuation=True) as executable:
        if args.stage != 'restore':
            create_checkpoint(executable, control, reader, damage=args.damage)
        if args.stage == 'create':
            return
        complete_checkpoint(executable, control, reader, cross_runner=args.stage == 'restore', damage=args.damage)


def create_checkpoint(executable, control, reader, *, damage=False):
        if not damage:
            subprocess.run(solver.command(str(executable), 'cae', 'noGUI=' + str(ROOT / 'build.py')), cwd=ROOT, check=True, timeout=120)
        process = solver.launch(str(executable), ['job=recover_probe', 'cpus=4', 'double=both', 'user=' + str(control), 'interactive'], ROOT, ROOT / 'first.log')
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if process.poll() is not None:
                if process.returncode:
                    raise RuntimeError('Probe solver failed before the checkpoint (exit ' +
                                       str(process.returncode) + '); inspect first.log')
                raise RuntimeError('Probe finished before interruption; recovery was not exercised')
            state = solver.sta_progress(ROOT / 'recover_probe.sta', .06)
            if state.get('simulation_time_s', 0) > (.01 if damage else .02):
                break
            time.sleep(.25)
        else:
            raise RuntimeError('Probe never reached a checkpoint')
        (ROOT / 'continuation.stop').touch()
        process.wait(timeout=90)
        verify_solver_cleanup(ROOT / 'first.log')
        if not (ROOT / 'continuation-stop-ack.json').exists():
            raise RuntimeError('Solver did not acknowledge orderly stop')
        (ROOT / 'continuation.stop').unlink()
        before = solver.sta_progress(ROOT / 'recover_probe.sta', .06)
        if before.get('solver_success'):
            raise RuntimeError('No interruption occurred')
        files = checkpoint_inventory(ROOT, 'recover_probe')
        receipt = dict(job_name='recover_probe', input_hash='probe', runtime_hash='probe', cpus=4, files=files)
        verify_checkpoint(ROOT, receipt, job_name='recover_probe', input_hash='probe', runtime_hash='probe', cpus=4)
        for p in ROOT.glob('*.lck'):
            p.unlink()
        subprocess.run(solver.command(str(executable), 'python', str(reader), str(ROOT / 'history-before.json')), cwd=ROOT, check=True, timeout=120)
        stopped = json.loads((ROOT / 'continuation-stop-ack.json').read_text())['simulation_time_s']
        original = json.loads((ROOT / 'history-before.json').read_text())
        integrity = history_continuity(original, {'duration': stopped, 'history_intervals': stopped / .00006})
        if not integrity['passed']:
            raise RuntimeError('Checkpoint lost original history before recover: ' + json.dumps(integrity))
        shutil.copy2(ROOT / 'recover_probe.sta', ROOT / 'before.sta')
        write_json(ROOT / 'checkpoint-receipt.json', {**receipt, 'before': before,
                   'checkpoint_history_integrity': integrity})


def complete_checkpoint(executable, control, reader, *, cross_runner=False, damage=False):
        receipt = json.loads((ROOT / 'checkpoint-receipt.json').read_text())
        verify_checkpoint(ROOT, receipt, job_name='recover_probe', input_hash='probe', runtime_hash='probe', cpus=4)
        files, before = receipt['files'], receipt['before']
        subprocess.run(solver.command(str(executable), 'job=recover_probe', 'recover', 'cpus=4', 'double=both', 'user=' + str(control), 'interactive'), cwd=ROOT, stdout=(ROOT / 'recover.log').open('w'), stderr=subprocess.STDOUT, check=True, timeout=600)
        after = solver.sta_progress(ROOT / 'recover_probe.sta', .06)
        if not after.get('solver_success') or after.get('simulation_time_s', 0) < .06:
            raise RuntimeError('Recovered probe did not finish the original step')
        subprocess.run(solver.command(str(executable), 'python', str(reader), str(ROOT / 'history-after.json')), cwd=ROOT, check=True, timeout=120)
        channels = json.loads((ROOT / 'history-after.json').read_text())
        continuity = history_continuity(channels, {'duration': .06, 'history_intervals': 1000})
        if not continuity['passed']:
            raise RuntimeError('Recovered history is incomplete: ' + json.dumps(continuity))
        original = json.loads((ROOT / 'history-before.json').read_text())
        final = {(r['step'], r['region'], r['variable']): r['data'] for r in channels}
        for row in original:
            values = final.get((row['step'], row['region'], row['variable']), [])
            if values[:len(row['data'])] != row['data']:
                raise RuntimeError('Recovery changed the existing history prefix')
        state_verified = verify_damage_state() if damage else False
        write_json(Path('output/explicit-recover-verification.json'), {'passed': True, 'cross_runner': cross_runner,
                   'damage_state_verified': state_verified, 'before': before, 'after': after, 'files': sorted(files), 'physics_changed': False, 'history_integrity': continuity,
                   'stop': json.loads((ROOT / 'continuation-stop-ack.json').read_text())})


def verify_damage_state():
    before = json.loads((ROOT / 'history-before.json.state.json').read_text())[-1]
    after = json.loads((ROOT / 'history-after.json.state.json').read_text())
    same_time = [row for row in after if math.isclose(row['time'], before['time'], rel_tol=1.e-7, abs_tol=1.e-10)]
    if not same_time:
        raise ValueError('Recovery lost the checkpoint field frame')
    fields = before['fields']
    for key, minimum in (('PEEQ', 0), ('SDEG', 0), ('JCCRT', .99), ('TEMP', 20)):
        if not fields.get(key) or max(fields[key]) <= minimum:
            raise ValueError('Recovery specimen did not exercise ' + key)
    if not fields.get('STATUS') or min(fields['STATUS']) < .5 or max(fields['SDEG']) >= .99:
        raise ValueError('Checkpoint must contain live material in progressive softening')
    if not any(abs(v) > 1.e-8 for value in fields.get('V', []) for v in value):
        raise ValueError('Recovery specimen was not moving')
    for key in ('U', 'V', 'PEEQ', 'SDEG', 'JCCRT', 'STATUS', 'TEMP'):
        old, new = fields[key], same_time[-1]['fields'].get(key)
        if new is None or len(old) != len(new):
            raise ValueError('Recovery changed state field dimensions: ' + key)
        for a, b in zip(old, new, strict=True):
            left = a if isinstance(a, list) else [a]
            right = b if isinstance(b, list) else [b]
            if any(not math.isclose(x, y, rel_tol=1.e-8, abs_tol=1.e-9) for x, y in zip(left, right, strict=True)):
                raise ValueError('Recovery changed checkpoint state: ' + key)
    if not any(value == 0 for row in after if row['time'] > before['time']
               for value in row['fields'].get('STATUS', [])):
        raise ValueError('Recovered damage specimen never reached deletion')
    return True


if __name__ == '__main__':
    main()
