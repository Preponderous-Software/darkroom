"""Contact sheets, captioned grids and the gallery's links and per-shot markers."""
import json

import pytest
from PIL import Image

from darkroom import encode as enc
from darkroom import gallery
from darkroom.cli import main
from darkroom.manifest import load, validate
from darkroom.shoot import shoot

# fake_capture fills the frame with an R,G,B colour: spring, summer, autumn, winter
SEASONS = ["120,200,120", "240,200,60", "200,100,40", "220,230,240"]


def quiet(*_):
    pass


def seasons_manifest(write_manifest, fake_cmd, root=None, **top):
    shots = [
        {"name": "season", "backend": "command", "device": {"width": 400, "height": 300},
         "each": SEASONS, "argv": fake_cmd + ["{out_dir}", "{width}", "{height}", "--color", "{value}"]},
        {"name": "walk", "kind": "clip", "backend": "command", "device": {"width": 400, "height": 300},
         "argv": fake_cmd + ["{out_dir}", "{width}", "{height}", "--frames", "6"], "gif": {"fps": 6},
         "caption": "Walking.", "alt": "Animation: walking"},
        {"name": "seasons", "kind": "grid", "of": "season", "title": "The four seasons",
         "captions": ["Spring", "Summer", "Autumn", "Winter"], "file": "docs/images/seasons.png"},
    ]
    data = {"vars": {}, "shots": shots, **top}
    return write_manifest(data)


def test_grid_of_an_each_still(write_manifest, fake_cmd, tmp_path):
    p = seasons_manifest(write_manifest, fake_cmd)
    assert shoot(load(p), log=quiet) == 0
    with Image.open(p.parent / "docs/images/seasons.png") as im:
        assert im.format == "PNG"
        w, h = im.size
        assert 700 <= w <= 1600  # 2x2 of 400 px tiles + gaps
        # the top-left tile is spring's green, the bottom-right winter's pale blue
        tl = im.convert("RGB").getpixel((w // 4, h // 2 - 40))
        br = im.convert("RGB").getpixel((3 * w // 4, h - 60))
        assert tl[1] > tl[0] + 40 and tl[1] > tl[2] + 40
        assert br[2] > 200 and br[0] > 180


def test_grid_from_tiles_and_validation(write_manifest, fake_cmd, tmp_path):
    p = seasons_manifest(write_manifest, fake_cmd)
    data = json.loads(p.read_text())
    Image.new("RGB", (400, 300), (10, 10, 10)).save(p.parent / "extra.png")
    data["shots"].append({"name": "row", "kind": "grid", "cols": 3,
                          "tiles": ["walk", {"shot": "walk", "frame": 0, "caption": "Start"},
                                    {"image": "extra.png", "caption": "Night"}]})
    p.write_text(json.dumps(data))
    assert shoot(load(p), log=quiet) == 0
    with Image.open(p.parent / "media/row.jpg") as im:
        assert im.width > im.height * 2  # one row of three
    probs = validate({"shots": [
        {"name": "a", "backend": "command", "argv": ["x"]},
        {"name": "g", "kind": "grid"},
        {"name": "h", "kind": "grid", "of": "nope"},
        {"name": "i", "kind": "grid", "tiles": [{"shot": "a", "image": "x.png"}, 3]},
        {"name": "j", "kind": "grid", "of": "g"},
    ]})
    assert any("g): a grid needs exactly one of 'of'" in e for e in probs)
    assert any("no shot named 'nope'" in e for e in probs)
    assert any("tiles[0]: needs exactly one of 'shot' or 'image'" in e for e in probs)
    assert any("tiles[1]: a shot name or an object" in e for e in probs)
    assert any("'g' is a grid, which has no take" in e for e in probs)


def test_grid_needs_a_take(write_manifest, fake_cmd):
    p = seasons_manifest(write_manifest, fake_cmd)
    logs = []
    assert shoot(load(p), only=["seasons"], log=logs.append) == 1
    assert any("no take of 'season' yet" in l for l in logs)


def test_encode_grid_has_no_numbers_and_title(tmp_path):
    ims = [Image.new("RGB", (300, 200), c) for c in ((200, 0, 0), (0, 200, 0), (0, 0, 200))]
    plain = enc.grid(ims)
    titled = enc.grid(ims, ["a", "b", "c"], "Title")
    assert plain.width == titled.width and titled.height > plain.height
    assert enc.grid(ims[:1], width=100).width <= 100


def test_contact_sheet_size_and_labels(tmp_path):
    frames = [Image.new("RGB", (1280, 720), (i * 20, 0, 0)) for i in range(8)]
    r = enc.contact_sheet(frames, tmp_path / "s.png")
    with Image.open(r.path) as im:
        assert 1500 <= im.width <= 1600
        labelled_h = im.height
    r = enc.contact_sheet(frames, tmp_path / "n.png", labels="none")
    with Image.open(r.path) as im:
        assert im.height < labelled_h


def test_sheet_cli_on_a_take_dir(write_manifest, fake_cmd):
    p = seasons_manifest(write_manifest, fake_cmd)
    shoot(load(p), only=["walk"], log=quiet)
    take = p.parent / ".darkroom/takes/walk"
    assert (take / "sheet.png").exists()  # the review sheet kept with the take
    before = set(take.iterdir())
    assert main(["sheet", str(take)]) == 0
    out = p.parent / "media/walk-sheet.png"
    with Image.open(out) as im:
        assert im.width > 1000  # 6 frames of 400 px, not one tiny thumbnail of sheet.png
    assert set(take.iterdir()) == before  # nothing written into the take
    assert main(["sheet", str(take), "-o", str(p.parent / "x.png"), "--labels", "none"]) == 0
    assert (p.parent / "x.png").exists()
    assert main(["sheet", str(p), "walk", "--labels", "caption"]) == 0
    # a plain folder: the sheet goes beside it, and other sheets in it are not frames
    folder = p.parent / "shots"
    folder.mkdir()
    for i in range(3):
        Image.new("RGB", (200, 100)).save(folder / f"{i}.png")
    Image.new("RGB", (10, 10)).save(folder / "sheet.png")
    assert main(["sheet", str(folder)]) == 0
    with Image.open(p.parent / "shots-sheet.png") as im:
        assert im.width == 3 * 200 + 4 * 4


def test_published_sheet_options(write_manifest, fake_cmd):
    p = seasons_manifest(write_manifest, fake_cmd)
    data = json.loads(p.read_text())
    data["shots"][1]["sheet"] = {"labels": "none", "cols": 6}
    p.write_text(json.dumps(data))
    assert shoot(load(p), only=["walk"], log=quiet) == 0
    with Image.open(p.parent / "media/walk-sheet.png") as im:
        assert 1400 <= im.width <= 1600 and im.width > 3 * im.height  # one row of six
    assert any("sheet.labels" in e for e in validate({"shots": [
        {"name": "a", "kind": "clip", "backend": "command", "argv": ["x"], "sheet": {"labels": "numbers"}}]}))


# ------------------------------------------------------------------ gallery links and markers

def repo_with_manifest(tmp_path, write_manifest, fake_cmd):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "tools/capture").mkdir(parents=True)
    data = {"out": "../../docs/media", "shots": [
        {"name": "title", "backend": "command", "device": {"width": 64, "height": 48},
         "argv": fake_cmd + ["{out_dir}", "{width}", "{height}"], "caption": "Title.", "alt": "The title"},
        {"name": "walk", "kind": "clip", "backend": "command", "device": {"width": 64, "height": 48},
         "argv": fake_cmd + ["{out_dir}", "{width}", "{height}", "--frames", "3"], "gif": {"fps": 4}}]}
    p = repo / "tools/capture/media.json"
    p.write_text(json.dumps(data))
    assert shoot(load(p), log=quiet) == 0
    return repo, p


def test_links_relative_inside_the_repo(tmp_path, write_manifest, fake_cmd):
    repo, p = repo_with_manifest(tmp_path, write_manifest, fake_cmd)
    md = gallery.markdown(load(p), repo / "README.md")
    assert "](docs/media/title.jpg)" in md


def test_doc_outside_the_repo_needs_base_url(tmp_path, write_manifest, fake_cmd, capsys):
    repo, p = repo_with_manifest(tmp_path, write_manifest, fake_cmd)
    pr = tmp_path / "elsewhere" / "pr.md"
    pr.parent.mkdir()
    pr.write_text("<!-- darkroom:start -->\n<!-- darkroom:end -->\n")
    assert main(["gallery", str(p), "--insert", str(pr)]) == 2
    assert "--base-url" in capsys.readouterr().err
    assert "../" not in pr.read_text() and str(tmp_path) not in pr.read_text()
    url = "https://raw.githubusercontent.com/OWNER/REPO/main/"
    assert main(["gallery", str(p), "--insert", str(pr), "--base-url", url]) == 0
    text = pr.read_text()
    assert f"]({url}docs/media/title.jpg)" in text and f"({url}docs/media/walk.gif)" in text
    assert str(tmp_path) not in text
    out = capsys.readouterr()
    assert main(["gallery", str(p), "--base-url", url.rstrip("/")]) == 0
    assert f"({url}docs/media/title.jpg)" in capsys.readouterr().out


def test_per_shot_markers(tmp_path, write_manifest, fake_cmd, capsys):
    repo, p = repo_with_manifest(tmp_path, write_manifest, fake_cmd)
    doc = repo / "docs" / "guide.md"
    doc.write_text("# Guide\n\nIntro.\n\n<!-- darkroom:shot title /-->\n\nMore prose.\n\n"
                   "<!-- darkroom:shot walk -->\nold image\n<!-- darkroom:end -->\n\nEnd.\n")
    assert main(["gallery", str(p), "--insert", str(doc)]) == 0
    text = doc.read_text()
    assert "old image" not in text and "Intro." in text and "More prose." in text and "End." in text
    assert "<!-- darkroom:shot title -->\n![The title](media/title.jpg)\n\n*Title.*\n<!-- darkroom:end -->" in text
    assert "![walk](media/walk.gif)" in text
    # idempotent: a second run changes nothing
    assert main(["gallery", str(p), "--insert", str(doc)]) == 0
    assert doc.read_text() == text
    doc.write_text("<!-- darkroom:shot titel /-->\n")
    assert main(["gallery", str(p), "--insert", str(doc)]) == 2
    assert "unknown shot(s) 'titel'" in capsys.readouterr().err
    doc.write_text("no markers\n")
    assert main(["gallery", str(p), "--insert", str(doc)]) == 2
