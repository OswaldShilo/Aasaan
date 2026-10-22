"""Books as a source: found at notebook creation, kept for the chat only, cited as [n] with a link."""
from conftest import FakeResp

DOCS = {"docs": [
    {"key": "/works/OL1W", "title": "Introduction to Algorithms", "author_name": ["Cormen"], "first_publish_year": 1990, "edition_count": 18, "subject": ["Algorithms"], "ia": ["introalgo"]},
    {"key": "/works/OL2W", "title": "Introduction to Algorithms", "edition_count": 2},                      # a duplicate title is dropped
    {"key": "/works/OL3W", "title": "Algorithms Unlocked", "author_name": ["Cormen"], "edition_count": 3, "first_sentence": ["Algorithms are everywhere."]}]}


def test_books_are_sourced_listed_and_kept_out_of_the_course(engine, worker, store, fake_web):
    fake_web.get(lambda url, p: FakeResp(200, data=DOCS) if "openlibrary.org/search.json" in url else None)
    nid = engine.create_notebook("L1", "Teach me algorithms", "en", [])
    worker.run_until_idle()
    books = [s for s in store.sources(nid) if s["kind"] == "book"]
    assert [b["title"] for b in books] == ["Introduction to Algorithms", "Algorithms Unlocked"] and books[0]["raw_ref"] == "https://openlibrary.org/works/OL1W"
    assert "Cormen" in books[0]["chunks"][0]["text"] and "Internet Archive" in books[0]["chunks"][0]["text"]
    view = engine.resources_view(nid)
    assert view["mine"] == [] and [f["kind"] for f in view["found"] if f["kind"] == "book"] == ["book", "book"]       # not "yours"; shown under Books
    assert not any(c["chunk_id"].startswith(books[0]["id"]) for c in engine._all_chunks(nid))                       # never course input
    assert any(c["chunk_id"].startswith(books[0]["id"]) for c in engine._all_chunks(nid, books=True))               # but the chat can use it
    chips = engine._ref_chips(nid, [f"{books[0]['id']}-p1-c1"], None, [f"{books[0]['id']}-p1-c1"])
    assert chips == [{"kind": "link", "url": "https://openlibrary.org/works/OL1W", "label": "Book · Introduction to Algorithms", "n": 1}]
    assert any(m["key"] == "books" for m in store.messages(nid))


def test_no_network_means_no_books_and_no_failure(engine, worker, store):
    nid = engine.create_notebook("L1", "Teach me algorithms", "en", [])
    worker.run_until_idle()
    assert engine._nb(nid)["status"] != "failed" and not [s for s in store.sources(nid) if s["kind"] == "book"]
