import pytest

from honeycomb_studio.solver import sta_progress

ROW = ' 165879 8.000E-03 8.000E-03 00:04:04 4.767E-08 53801 8.714E+03 8.478E+04\n'
SUCCESS = '  THE ANALYSIS HAS COMPLETED SUCCESSFULLY\n'
FAILURE = '  THE ANALYSIS HAS NOT BEEN COMPLETED\n'
HEADER = 'Abaqus/Explicit 2020  DATE 06-Nov-2026  TIME 23:48:19\n'


@pytest.mark.parametrize('history,current,success,error', [
    (ROW + FAILURE, HEADER + ROW + SUCCESS, True, False),
    (ROW + SUCCESS, HEADER + ROW + FAILURE, False, True),
    (ROW + SUCCESS, HEADER, False, False),
    (ROW + SUCCESS, HEADER + ROW, False, False),
    ('', ROW + SUCCESS + ROW, False, False),
    ('', ROW + SUCCESS + FAILURE, False, True),
])
def test_status_belongs_to_latest_native_invocation(tmp_path, history, current, success, error):
    sta = tmp_path / 'original.sta'
    sta.write_text(history + current)
    status = sta_progress(sta, .008)
    assert status['solver_success'] is success
    assert status['solver_error'] is error


def test_real_checkpoint_then_completed_recovery(tmp_path):
    # Same final status sequence and numerical row as development-0053's
    # immutable original artifact; its previous segment stopped at 7.842 ms.
    sta = tmp_path / 'original.sta'
    sta.write_text(ROW.replace('8.000E-03', '7.842E-03') + FAILURE + '\n' + HEADER + ROW + SUCCESS)
    status = sta_progress(sta, .008)
    assert status['solver_success']
    assert status['simulation_time_s'] == .008
    assert status['increment'] == 165879
