"""Legend reader: finding the legend, rows, symbols, engineer tags, defaults,
coordinates, edits, saved legends and the CLI."""

from __future__ import annotations

import json

import numpy as np
import pikepdf
import pytest

from pinny.legend import (
    EngineerLibrary,
    Legend,
    LegendError,
    compare,
    find_legends,
    guess_group,
    nice_name,
    read_legend,
    signature_for_box,
)
from pinny.legend.__main__ import main as cli

from .pdfgen import MAIN_ROWS, PAGE, Canvas, Layout, Row, build, draw_legend, floor_plan


def make_set(tmp_path, rows=MAIN_ROWS, layout=None, rotate=0, crop=None, name="set.pdf", notes=True):
    """A two-page set: a floor plan, then the legend (drawn upright on the
    displayed page). Returns (path, centres)."""
    plan = Canvas()
    floor_plan(plan)
    w, h = (crop[2] - crop[0], crop[3] - crop[1]) if crop else PAGE
    leg = Canvas(*((h, w) if rotate in (90, 270) else (w, h)))
    centres = draw_legend(leg, rows, layout or Layout(), notes=notes)
    path = tmp_path / name
    build([(plan, 0, None), (leg, rotate, crop)], str(path))
    return path, centres


def centre_px(path, page_index, pt_topdown):
    """Canonical px of a point given top-down in points on the displayed page."""
    x, y = pt_topdown
    return (x * 200 / 72, y * 200 / 72)


def inside(box, pt, pad=2.0):
    return box[0] - pad <= pt[0] <= box[2] + pad and box[1] - pad <= pt[1] <= box[3] + pad


@pytest.fixture(scope="module")
def main_set(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("legend")
    path, centres = make_set(tmp)
    return path, centres, read_legend(path, engineer="Hanson & Reyes")


# --------------------------------------------------------------------------
# Reading


def test_finds_legend_page_and_skips_floor_plan(main_set):
    path, _, legend = main_set
    found = find_legends(path)
    assert [c.page_index for c in found] == [1]
    assert found[0].heading == "ELECTRICAL SYMBOLS LEGEND"
    assert legend.page_index == 1 and legend.engineer == "Hanson & Reyes"
    assert legend.pdf_sha256 and legend.coordinate_frame["dpi"] == 200


def test_reads_every_row_and_stops_at_notes(main_set):
    _, _, legend = main_set
    descs = [e.description for e in legend.entries]
    assert descs == [
        'DUPLEX RECEPTACLE, 20A, 125V, +18" AFF U.O.N.',
        "GFCI DUPLEX RECEPTACLE, 20A, 125V",
        "WEATHERPROOF GFCI RECEPTACLE, IN-USE COVER",
        "QUAD RECEPTACLE (LEFT) / USB CHARGING RECEPTACLE, TYPE A/C (RIGHT)",
        "DEDICATED RECEPTACLE, ISOLATED CIRCUIT",
        "DISCONNECT SWITCH, FUSED",
        "THERMOSTAT (BY MECH)",
        "HOMERUN TO PANEL, HASH MARKS = CONDUCTORS",
    ]
    assert not any("TAMPER" in d for d in descs)


def test_flags_combined_and_wrapped_rows(main_set):
    _, _, legend = main_set
    quad = legend.entries[3]
    assert "combined" in quad.flags and "wrapped" in quad.flags and len(quad.symbol_boxes) == 2
    assert quad.status == "check"
    assert [e.id for e in legend.needs_review()] == [quad.id]
    assert all(len(e.symbol_boxes) == 1 for e in legend.entries if e is not quad)


def test_symbol_labels_become_engineer_tags(main_set):
    _, _, legend = main_set
    tags = {e.description.split(",")[0]: (e.tag, e.tag_source) for e in legend.entries}
    assert tags["GFCI DUPLEX RECEPTACLE"] == ("GFI", "symbol_label")
    assert tags["WEATHERPROOF GFCI RECEPTACLE"] == ("WP", "symbol_label")
    duplex = legend.entries[0]
    assert duplex.tag_source == "generated" and "generated_tag" in duplex.flags
    assert len({e.tag for e in legend.entries}) == len(legend.entries)


def test_not_counted_defaults_and_groups(main_set):
    _, _, legend = main_set
    by = {e.description.split(",")[0].split(" (")[0]: e for e in legend.entries}
    assert not by["THERMOSTAT"].count and "another trade" in by["THERMOSTAT"].not_counted_reason
    assert not by["HOMERUN TO PANEL"].count and "wiring" in by["HOMERUN TO PANEL"].not_counted_reason.lower()
    assert by["DUPLEX RECEPTACLE"].group == "Receptacles"
    assert by["DISCONNECT SWITCH"].group == "Equipment"  # from the EQUIPMENT section heading
    assert len(legend.counted()) == 6


def test_symbol_boxes_surround_the_drawings(main_set):
    path, centres, legend = main_set
    for e in legend.entries:
        pts = centres[[k for k in centres if e.description.startswith(k[:20])][0]]
        for box, pt in zip(sorted(e.symbol_boxes), sorted(pts)):
            assert inside(box, centre_px(path, 1, pt)), (e.name, box)


@pytest.mark.parametrize("rotate,crop", [(90, None), (180, (20, 30, 1200, 780)), (270, None)])
def test_rotated_and_cropped_pages(tmp_path, main_set, rotate, crop):
    _, _, ref = main_set
    path, centres = make_set(tmp_path, rotate=rotate, crop=crop)
    legend = read_legend(path)
    assert [e.description for e in legend.entries] == [e.description for e in ref.entries]
    assert [e.tag for e in legend.entries] == [e.tag for e in ref.entries]
    for e in legend.entries:
        pts = centres[[k for k in centres if e.description.startswith(k[:20])][0]]
        for pt in pts:
            assert any(inside(b, centre_px(path, 1, pt)) for b in e.symbol_boxes), (rotate, e.name)


def test_tag_column(tmp_path):
    rows = [
        Row(["duplex"], ["DUPLEX RECEPTACLE, 20A"], tag="D"),
        Row(["gfci"], ["GFCI RECEPTACLE"], tag="G"),
        Row(["floor"], ["FLOOR BOX, FLUSH"], tag="FB"),
        Row(["jbox"], ["JUNCTION BOX"], tag="J"),
    ]
    path, _ = make_set(tmp_path, rows, Layout(sym_x=70, tag_x=112, desc_x=150))
    legend = read_legend(path)
    assert [(e.tag, e.tag_source) for e in legend.entries] == [
        ("D", "tag_column"), ("G", "tag_column"), ("FB", "tag_column"), ("J", "tag_column")]
    assert [e.description for e in legend.entries] == [r.text[0] for r in rows]
    assert not legend.needs_review()


def test_tag_written_in_description(tmp_path):
    rows = [
        Row(["duplex"], ["D1 - DUPLEX RECEPTACLE, 20A"]),
        Row(["gfci"], ["G1 - GFCI RECEPTACLE"]),
        Row(["quad"], ["Q1 - QUAD RECEPTACLE"]),
    ]
    path, _ = make_set(tmp_path, rows)
    legend = read_legend(path)
    assert [(e.tag, e.tag_source, e.description) for e in legend.entries] == [
        ("D1", "description", "DUPLEX RECEPTACLE, 20A"),
        ("G1", "description", "GFCI RECEPTACLE"),
        ("Q1", "description", "QUAD RECEPTACLE"),
    ]


def test_legend_without_headers_or_rules(tmp_path):
    rows = [Row(["duplex"], ["DUPLEX RECEPTACLE"]), Row(["quad"], ["QUAD RECEPTACLE"]),
            Row(["jbox"], ["JUNCTION BOX"]), Row(["disc"], ["DISCONNECT SWITCH"])]
    path, _ = make_set(tmp_path, rows, Layout(headers=False, rules=False, heading="LEGEND"))
    legend = read_legend(path)
    assert [e.description for e in legend.entries] == [r.text[0] for r in rows]


def test_two_column_legend(tmp_path):
    left = [Row(["duplex"], ["DUPLEX RECEPTACLE"]), Row(["gfci"], ["GFCI RECEPTACLE"], label="GFI")]
    right = [Row(["quad"], ["QUAD RECEPTACLE"]), Row(["jbox"], ["JUNCTION BOX"]), Row(["disc"], ["DISCONNECT SWITCH"])]
    leg = Canvas()
    draw_legend(leg, left, Layout(width=420), notes=False)
    # A second column to the right, under the same heading.
    lay2 = Layout(x=470, sym_x=505, desc_x=580, width=420, heading="")
    y = lay2.y + 34
    leg.text(lay2.sym_x - 20, y, "SYMBOL", size=7)
    leg.text(lay2.desc_x, y, "DESCRIPTION", size=7)
    y += 5
    for r in right:
        cy = y + 11
        leg.symbol(r.syms[0], lay2.sym_x, cy)
        leg.text(lay2.desc_x, y + 14, r.text[0])
        y += 22
    path = tmp_path / "two.pdf"
    build([(leg, 0, None)], str(path))
    legend = read_legend(path)
    assert sorted(e.description for e in legend.entries) == sorted(r.text[0] for r in left + right)


def test_errors(tmp_path):
    plan = Canvas()
    floor_plan(plan)
    path = tmp_path / "plan.pdf"
    build([(plan, 0, None)], str(path))
    assert find_legends(path) == []
    with pytest.raises(LegendError) as e:
        read_legend(path)
    assert e.value.code == "legend_not_found"
    with pytest.raises(LegendError) as e:
        read_legend(path, page_index=5)
    assert e.value.code == "page_index_out_of_range"


def test_scanned_page_is_refused(tmp_path):
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    img = pikepdf.Stream(pdf, bytes(64 * 64))
    img.Type, img.Subtype, img.Width, img.Height = pikepdf.Name.XObject, pikepdf.Name.Image, 64, 64
    img.ColorSpace, img.BitsPerComponent = pikepdf.Name.DeviceGray, 8
    page.obj.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=img))
    page.obj.Contents = pdf.make_stream(b"q 612 0 0 792 0 0 cm /Im0 Do Q")
    path = tmp_path / "scan.pdf"
    pdf.save(path)
    with pytest.raises(LegendError) as e:
        read_legend(path, page_index=0)
    assert e.value.code == "raster_page"


# --------------------------------------------------------------------------
# Editing


@pytest.fixture()
def legend(main_set):
    return Legend.from_dict(main_set[2].to_dict())  # a fresh copy per test


def test_split_combined_row(legend):
    quad = legend.entries[3]
    a, b = legend.split(quad.id)
    assert (a.name, a.description) == ("Quad receptacle", "QUAD RECEPTACLE")
    assert (b.name, b.description) == ("USB charging receptacle", "USB CHARGING RECEPTACLE, TYPE A/C")
    assert a.symbol_boxes[0][0] < b.symbol_boxes[0][0]
    assert a.status == b.status == "added" and not legend.needs_review()
    assert legend.edits[-1]["op"] == "split"
    with pytest.raises(LegendError):
        legend.split(a.id)


def test_confirm_and_rename_clear_checks(legend):
    quad = legend.entries[3]
    legend.confirm(quad.id)
    assert quad.status == "ok" and not legend.needs_review()


def test_tags_are_editable_and_duplicates_are_flagged(legend):
    d = legend.entries[0]
    legend.set_tag(d.id, "d")
    assert d.tag == "D" and d.tag_source == "user" and "generated_tag" not in d.flags
    legend.set_tag(legend.entries[1].id, "D")
    assert {e.id for e in legend.needs_review()} >= {d.id, legend.entries[1].id}
    legend.set_tag(legend.entries[1].id, "G")
    assert "duplicate_tag" not in d.flags
    with pytest.raises(LegendError):
        legend.set_tag(d.id, "not a tag!")


def test_merge_delete_count_group(legend):
    gfci, wp = legend.entries[1], legend.entries[2]
    merged = legend.merge(wp.id, gfci.id)
    assert len(merged.symbol_boxes) == 2 and wp not in legend.entries
    legend.set_count(merged.id, False, "Owner furnished")
    assert not merged.count and merged.not_counted_reason == "Owner furnished"
    legend.set_group(merged.id, "Power")
    assert merged.group == "Power"
    n = len(legend.entries)
    legend.delete(legend.entries[0].id)
    assert len(legend.entries) == n - 1
    assert [e["op"] for e in legend.edits] == ["merge", "set_count", "set_group", "delete"]


def test_add_missed_symbol_reads_its_row(legend):
    disc = next(e for e in legend.entries if e.description.startswith("DISCONNECT"))
    box = disc.symbol_boxes[0]
    legend.delete(disc.id)
    added = legend.add(box)
    assert added.description == "DISCONNECT SWITCH, FUSED" and added.name == "Disconnect switch"
    assert added.status == "added" and added.group == "Equipment" and added.tag_source == "generated"
    with pytest.raises(LegendError) as e:
        legend.add(legend.entries[0].symbol_boxes[0])
    assert e.value.code == "row_already_read"
    extra = legend.add(legend.entries[0].symbol_boxes[0], tag="D2", force=True)
    assert extra.tag == "D2"


def test_save_and_load_round_trip(legend, tmp_path):
    legend.split(legend.entries[3].id)
    legend.set_tag(legend.entries[0].id, "D")
    path = tmp_path / "legend.json"
    legend.save(path)
    again = Legend.load(path)
    assert again.to_dict() == legend.to_dict()
    data = json.loads(path.read_text())
    assert data["format"] == "pinny.legend" and data["format_version"] == 1
    new = again.add((10, 10, 40, 40), name="Manual", tag="MAN", force=True)
    assert new.id not in {e.id for e in legend.entries}


# --------------------------------------------------------------------------
# Saved legends per engineer


def test_library_compare_and_update(main_set, tmp_path):
    path, _, legend = main_set
    lib = EngineerLibrary("Hanson & Reyes")
    assert len(compare(legend, lib)["new"]) == len(legend.entries) and not compare(legend, lib)["match"]
    lib.update_from(legend, project="Oak Ridge School")
    diffs = compare(legend, lib)
    assert len(diffs["match"]) == len(legend.entries) and not diffs["new"] and not diffs["missing"]

    # Next project: one row reworded, one new, one dropped, one drawn differently.
    rows = list(MAIN_ROWS)
    rows[4] = Row(["dedicated"], ["DEDICATED RECEPTACLE, ISOLATED GROUND"])
    rows[5] = Row(["jbox"], ["DISCONNECT SWITCH, FUSED"], section="EQUIPMENT")
    rows = rows[:6] + [Row(["floor"], ["FLOOR BOX, FLUSH"])] + rows[7:]
    p2, _ = make_set(tmp_path, rows, name="next.pdf")
    nxt = read_legend(p2)
    lib.save(tmp_path / "lib.json")
    diffs = compare(nxt, EngineerLibrary.load(tmp_path / "lib.json"))
    changed = {d.name: d.notes for d in diffs["changed"]}
    assert any("Wording changed" in n for n in changed["Dedicated receptacle"])
    assert any("drawn differently" in n for n in changed["Disconnect switch"])
    assert [d.name for d in diffs["new"]] == ["Floor box"]
    assert [d.name for d in diffs["missing"]] == ["Thermostat (by mech)"]


def test_signature_for_hand_drawn_box(main_set):
    path, _, legend = main_set
    e = legend.entries[0]
    assert signature_for_box(path, 1, e.symbol_boxes[0]) == e.signatures[0]


# --------------------------------------------------------------------------
# Helpers and CLI


def test_nice_name_and_groups():
    assert nice_name('DUPLEX RECEPTACLE, 20A, 125V') == "Duplex receptacle"
    assert nice_name("240V RECEPTACLE, NEMA 14-50") == "240V receptacle"
    assert nice_name("GFCI DUPLEX RECEPTACLE") == "GFCI duplex receptacle"
    assert guess_group("DATA OUTLET, (2) CAT6") == "Low voltage"
    assert guess_group("PANELBOARD, SURFACE MOUNTED") == "Equipment"
    assert guess_group("CONDUIT CONCEALED") == "Wiring"
    assert guess_group("SOMETHING ELSE") == "Other"


def test_cli(main_set, tmp_path, capsys):
    path, _, legend = main_set
    assert cli(["find", str(path)]) == 0
    out = tmp_path / "l.json"
    assert cli(["read", str(path), "--engineer", "H&R", "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "GFI" in text and "need a look" in text and out.exists()
    lib = EngineerLibrary("H&R")
    lib.update_from(legend)
    lib.save(tmp_path / "lib.json")
    capsys.readouterr()
    assert cli(["compare", str(out), str(tmp_path / "lib.json")]) == 0
    assert len(json.loads(capsys.readouterr().out)["match"]) == len(legend.entries)
    plan = Canvas()
    floor_plan(plan)
    build([(plan, 0, None)], str(tmp_path / "p.pdf"))
    assert cli(["find", str(tmp_path / "p.pdf")]) == 2
    assert cli(["bogus"]) == 64
