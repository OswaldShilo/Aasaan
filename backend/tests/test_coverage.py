"""The outline covers the learner's own contents list: found, mapped, gaps reported."""
from app import coverage

CH = [{"id": 1, "position": 1, "title": "Arrays and memory", "summary": "How arrays store items", "objectives": ["index an array"], "prereqs": []},
      {"id": 2, "position": 2, "title": "Linked lists", "summary": "Nodes and pointers", "objectives": ["build a list"], "prereqs": [1]},
      {"id": 3, "position": 3, "title": "Trees", "summary": "Hierarchies", "objectives": [], "prereqs": [4]},
      {"id": 4, "position": 4, "title": "Sorting", "summary": "Ordering items", "objectives": ["sort"], "prereqs": []}]


def test_a_contents_list_is_found_whether_it_is_lines_or_flattened():
    lines = [{"text": "Contents\n1. Arrays and memory ..... 3\n2. Linked lists ..... 9\n3. Trees ..... 21\n4. Hash tables ..... 30"}]
    flat = [{"text": "Course plan 1. Arrays and memory 2. Linked lists 3. Trees 4. Hash tables 5. Graphs"}]
    assert coverage.checklist_from_chunks(lines) == ["Arrays and memory", "Linked lists", "Trees", "Hash tables"]
    assert coverage.checklist_from_chunks(flat)[-1] == "Graphs" and len(coverage.checklist_from_chunks(flat)) == 5
    assert coverage.checklist_from_chunks([{"text": "Just a paragraph about arrays."}]) == []


def test_each_item_maps_to_a_chapter_and_gaps_are_named():
    cov = coverage.coverage_map(["Arrays and memory", "Linked lists", "Hash tables", "Graph algorithms"], CH)
    assert [(r["item"], r["position"]) for r in cov["map"]] == [("Arrays and memory", 1), ("Linked lists", 2)]
    assert cov["gaps"] == ["Hash tables", "Graph algorithms"]


def test_outline_problems_are_reported_in_plain_terms():
    assert coverage.validate_outline(CH) == ["chapter 3 has no objectives", "chapter 3 depends on a later chapter"]


def test_the_engine_says_whether_everything_is_covered(engine, worker, store):
    nid = engine.create_notebook("L1", "Teach me data structures", "en", [])
    worker.run_until_idle()
    for c in store.chapters(nid):
        store.update_chapter(c["id"], title=["Arrays and memory", "Linked lists", "Trees", "Hash tables", "Graphs", "Sorting", "Search", "Recursion"][(c["position"] - 1) % 8])
    cov = engine._check_coverage(nid, [{"text": "1. Arrays and memory 2. Linked lists 3. Trees 4. Hash tables"}])
    assert cov["gaps"] == []
    assert any(m["key"] == "coverage" for m in store.messages(nid)) and store.cache_get(f"coverage:{nid}")["items"][0] == "Arrays and memory"
