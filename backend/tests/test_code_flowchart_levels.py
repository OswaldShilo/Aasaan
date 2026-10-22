"""Code flowcharts: a high-level view in plain English, a detailed view, and the source lines behind every node."""
from app.visuals import astflow, render, specs

BS = '''def binary_search(a, x):
    low = 0
    high = len(a) - 1
    while low <= high:
        mid = (low + high) // 2
        if a[mid] == x:
            return mid
        elif a[mid] < x:
            low = mid + 1
        else:
            high = mid - 1
    return -1
'''


def test_high_level_view_is_plain_english_and_every_node_knows_its_lines():
    lv = astflow.code_to_levels(BS, "Binary search")
    labels = [n["label"] for n in lv["high"]["nodes"]]
    assert "Repeat while low <= high?" in labels and "Work out mid" in labels and "Give back mid" in labels and "a[mid] equals x?" in labels
    assert len(labels) <= 12 and all(len(l.replace("?", "").split()) <= 5 for l in labels)
    assert lv["lines"]["high"]["n4"] == [4, 4] and lv["lines"]["high"]["n3"] == [2, 3]            # setup covers lines 2-3, the loop test is line 4
    assert [n["label"] for n in lv["detailed"]["nodes"]][3:5] == ["low <= high?", "mid = (low + high)"]    # detailed keeps the code's own wording
    assert lv["code"].startswith("def binary_search")


def test_levels_render_with_clickable_node_groups_and_bad_code_is_refused():
    lv = astflow.code_to_levels(BS, "Binary search")
    svg = render.render(specs.parse_spec(lv["high"])).svg
    assert svg.count('class="fn" data-n="') == len(lv["high"]["nodes"]) and 'data-n="n4"' in svg
    assert astflow.code_to_levels("def broken(:", "x") is None and astflow.code_to_levels("import os", "x") is None


def test_the_illustrator_ships_both_views_without_a_model_call(engine):
    from app.agents.illustrator import Section, build_one
    blk, how = build_one(engine.router, engine.store, "code_flowchart", "Binary search", Section("s1", "Searching", "text", BS), "en")
    p = blk["content"]["payload"]
    assert "no model call" in how and p["level"] == "high" and set(p["levels"]) == {"high", "detailed"} and p["code"].startswith("def binary_search")
    assert 'class="fn"' in p["levels"]["detailed"]["svg"] and p["lines"] == p["levels"]["high"]["lines"]
