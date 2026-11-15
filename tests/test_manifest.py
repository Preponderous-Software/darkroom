import pytest

from darkroom.manifest import DEVICES, ManifestError, load, template, validate


def base(**shot):
    s = {"name": "hall", "kind": "still", "backend": "web", "url": "http://x/{base}", **shot}
    return {"vars": {"base": "game"}, "shots": [s]}


def test_valid_minimal():
    assert validate(base()) == []


def test_template_leaves_unknown_braces():
    assert template("{a}/{b} {x: 1}", {"a": 1}) == "1/{b} {x: 1}"
    assert template(["{a}", {"k": "{a}"}], {"a": "z"}) == ["z", {"k": "z"}]


def test_unknown_key_suggests():
    probs = validate(base(captoin="x"))
    assert any("unknown key 'captoin'" in p and "caption" in p for p in probs)


def test_bad_step_named_with_path():
    probs = validate(base(steps=[{"js": "1"}, {"kye": "KeyW"}]))
    assert any(p.startswith("shots[0] (hall).steps[1]") and "'key'" in p for p in probs)


def test_nested_repeat_steps_validated():
    probs = validate(base(kind="clip", steps=[{"repeat": 3, "steps": [{"tap": [1]}]}]))
    assert any("steps[0].steps[0].tap" in p for p in probs)


def test_camera_needs_strata_kit():
    assert any("camera" in p for p in validate(base(steps=[{"camera": {"yaw": 1}}])))
    assert validate(base(adapter="strata-kit", steps=[{"camera": {"yaw": 1}}])) == []


def test_unknown_device_and_var():
    probs = validate(base(device="phnoe", url="http://x/{bse}"))
    assert any("unknown device 'phnoe'" in p and "phone" in p for p in probs)
    assert any("unknown variable '{bse}'" in p for p in probs)


def test_duplicate_names_and_pair_target():
    d = base()
    d["shots"].append({"name": "hall", "url": "u"})
    d["shots"].append({"name": "p", "kind": "pair", "of": "nope"})
    probs = validate(d)
    assert any("duplicate" in p for p in probs)
    assert any("no shot named 'nope'" in p for p in probs)


def test_command_requires_argv():
    probs = validate({"shots": [{"name": "a", "backend": "command"}]})
    assert any("argv" in p for p in probs)


def test_still_cannot_have_gif():
    assert any("only clips" in p for p in validate(base(gif={"fps": 10})))


def test_load_vars_override_defaults_and_devices(write_manifest):
    p = write_manifest({"vars": {"base": "a"}, "devices": {"wide": {"width": 960, "height": 540}},
                        "defaults": {"device": "wide", "caption": "c"},
                        "shots": [{"name": "s", "url": "{base}"}, {"name": "t", "url": "u", "device": "phone"}]})
    m = load(p, {"base": "b"})
    assert m.vars["base"] == "b"
    s, t = m.shots
    assert m.device(s["device"])["width"] == 960 and s["caption"] == "c"
    assert m.device(t["device"]) == DEVICES["phone"]
    assert m.budgets() == {"still_kb": 200, "gif_mb": 3.0, "mp4_kb": 600, "webm_kb": 400}
    assert m.shot_hash(s, None) != m.shot_hash(s, "before")


def test_load_reports_json_error(write_manifest, tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{\n  \"shots\": [,]\n}")
    with pytest.raises(ManifestError) as e:
        load(p)
    assert "bad.json:2" in str(e.value)


def test_example_manifests_are_valid():
    from pathlib import Path
    for p in (Path(__file__).parent.parent / "examples").glob("*/media.json"):
        load(p)


def test_vars_can_refer_to_vars(write_manifest):
    p = write_manifest({"vars": {"page": "file://{manifest_dir}/i.html", "u": "{page}?x"},
                        "shots": [{"name": "a", "url": "{u}"}]})
    m = load(p)
    assert m.vars["u"] == f"file://{p.parent}/i.html?x"
