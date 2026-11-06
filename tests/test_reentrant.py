import math

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from honeycomb_studio.adapters.reentrant import bond_faces, network
from honeycomb_studio.api import make_app
from honeycomb_studio.engine import Engine
from honeycomb_studio.estimates import predict
from honeycomb_studio.models import Axis, BatchSpec, ModelSpec, expand
from honeycomb_studio.store import Store


def test_reference_section_has_same_mass_and_walls_as_solved_image1():
    walls = network(100, 20, 6.35, 5, 1.25)
    assert len(walls) == 234
    assert sum(double for _, _, double in walls) == 74
    area = sum(math.dist(u, v) * 100 * .0635 * (2 if double else 1) for u, v, double in walls)
    assert area * 2700e-9 == pytest.approx(.0226350620948468)
    for u, v, _ in walls:
        assert -50 <= u[0] <= 50 and -50 <= v[0] <= 50
        assert -10 <= u[1] <= 10 and -10 <= v[1] <= 10
        assert not (u[1] == v[1] and abs(u[1]) == 10)


def test_clipped_short_struts_do_not_prevent_bonding_end_faces_within_existing_tolerance():
    walls = network(100, 20, 5.69160327203117, 3.325791235375995, .8646182243641487)
    vertices, faces = [], []
    for u, v, _double in walls:
        start = len(vertices)
        vertices.extend(((u[0], u[1], 0.), (v[0], v[1], 0.),
                         (v[0], v[1], 100.), (u[0], u[1], 100.)))
        faces.append(tuple(range(start, start + 4)))
    assert max(p[1] for p in vertices) == pytest.approx(9.9773737)
    for target in (-10., 10.):
        coordinate, indices = bond_faces(vertices, faces, target, .05)
        assert abs(target - coordinate) == pytest.approx(.0226263)
        assert indices
        assert all(abs(vertices[j][1] - coordinate) < 1.e-5 for i in indices for j in faces[i])
    with pytest.raises(ValueError, match='tolerance'):
        bond_faces(vertices, faces, 10., .01)


def test_resolve_uses_reentrant_density_not_hexagonal_density():
    spec = ModelSpec(core_topology="reentrant_0")
    resolved = spec.resolved()
    assert resolved["core_cell_plane"] == "XZ"
    assert resolved["core_extrusion_axis"] == "Y"
    assert resolved["reentrant_angle_to_z_deg"] == pytest.approx(26.565051177)
    expected = (2 * 6.35 + 2 * math.hypot(1.25, 2.5)) * .0635 * 2700 / (5.1 * 5)
    assert resolved["periodic_density_kg_m3"] == pytest.approx(expected)
    assert resolved["periodic_density_kg_m3"] != 72
    assert resolved["impact_mass"] == ModelSpec().resolved()["impact_mass"]
    assert resolved["velocity"] == ModelSpec().resolved()["velocity"]


@pytest.mark.parametrize("changes", [
    {"reentrant_indent": 3.175}, {"reentrant_indent": 3.17},
    {"core_angle": 90}, {"phase_y": 1}, {"phase_z": 6},
])
def test_invalid_or_unsupported_auxetic_geometry_is_rejected(changes):
    with pytest.raises(ValidationError):
        ModelSpec(core_topology="reentrant_0", **changes)


def test_design_identity_distinguishes_topology_and_cell_shape_but_not_mesh():
    runs = expand(BatchSpec(sampling="table", designs=[
        {}, {"core_topology": "reentrant_0"},
        {"core_topology": "reentrant_0", "reentrant_cell_height": 6},
        {"core_topology": "reentrant_0", "mesh_preset": "fine"},
    ]))
    assert len(runs) == 4
    assert len({r["design_id"] for r in runs}) == 3
    assert runs[1]["design_id"] == runs[3]["design_id"]
    with pytest.raises(ValueError, match="ô lõm"):
        expand(BatchSpec(axes=[Axis(parameter="reentrant_indent", values=[1, 1.25])]))


def test_hexagonal_calibration_is_not_used_for_auxetic_estimate():
    old = {"model": ModelSpec().resolved(), "durations": {"solving": 60}}
    result = predict(ModelSpec(core_topology="reentrant_0").resolved(), [old])
    assert result["calibration_runs"] == 0
    assert result["solver_minutes"] > 25


@pytest.mark.parametrize("key,value", [("core_angle", 0), ("phase_y", 0), ("core_z_divisions", 20)])
def test_auxetic_rejects_axes_that_do_not_change_its_geometry(key, value):
    with pytest.raises(ValueError, match="không áp dụng"):
        expand(BatchSpec(model=ModelSpec(core_topology="reentrant_0", mesh_preset="custom"),
                         axes=[Axis(parameter=key, values=[value])]))


def test_auxetic_template_and_preview_round_trip(tmp_path):
    store = Store(tmp_path)
    engine = Engine(store, fake=True)
    with TestClient(make_app(store, engine, "test", tmp_path / "config", start_worker=False)) as client:
        headers = {"X-Honeycomb-Token": "test"}
        template = client.get("/v1/template", headers=headers).json()
        assert any(p["key"] == "core_topology" for p in template["parameters"])
        spec = template["spec"]
        spec["model"]["core_topology"] = "reentrant_0"
        response = client.post("/v1/preview", headers=headers, json=spec)
        assert response.status_code == 200
        row = response.json()["runs"][0]
        assert row["core_topology"] == "reentrant_0"
        assert row["core_cell_plane"] == "XZ"
        created = client.post("/v1/batches", headers=headers, json=spec)
        assert created.status_code == 200
