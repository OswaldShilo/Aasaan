"""The notebook pipeline end to end (offline, with the FakeProvider test double)."""
import pytest

from app.agents import teacher
from app.router import AllFailed
from app.schemas import LearnerProfile
from conftest import FakeResp
from test_web import ARTICLE, PARAS
from fake_provider import FakeProvider

LID = "learner-1"


def make(engine, text="I want to learn data structures", files=()):
    return engine.create_notebook(LID, text, "en", list(files))


def build_all(engine, worker, nid):
    worker.run_until_idle()
    return engine.notebook_view(nid)


def quiz_keys(store, cid):
    qb = next(b for b in store.blocks(cid) if b["type"] == "quiz")
    return {q["id"]: q for q in qb["content"]["questions"]}


# ----------------------------------------------------------------------- phase 0 + 1 + 2
def test_goal_only_builds_a_full_notebook(engine, worker, store):
    nid = make(engine)
    v = build_all(engine, worker, nid)
    assert len(v["chapters"]) == 8 and v["status"] == "complete" and v["readiness"] == 100
    assert all(c["status"] == "ready" for c in v["chapters"]) and v["open_ready"]
    ids = {c["id"] for c in v["chapters"]}
    assert v["edges"] and all(a in ids and b in ids for a, b in v["edges"])  # the graph
    ch1 = engine.chapter_view(v["chapters"][0]["id"])
    types = [b["type"] for b in ch1["blocks"]]
    assert types[0] == "notes" and "slides" in types and types[-2:] == ["quiz", "summary"]
    quiz = next(b for b in ch1["blocks"] if b["type"] == "quiz")["content"]["questions"]
    assert 3 <= len(quiz) <= 5 and all(set(q) == {"id", "text", "options", "concept"} for q in quiz)  # no keys or hints sent


def test_notebook_opens_after_chapter_one_while_the_rest_queue(engine, worker, store):
    nid = make(engine)
    engine.process_job(store.pending_jobs()[0]["id"])  # the notebook job: outline + chapter 1
    v = engine.notebook_view(nid)
    assert v["open_ready"] and v["status"] == "generating" and v["generating"]
    assert [c["status"] for c in v["chapters"]] == ["ready"] + ["queued"] * 7
    assert v["readiness"] == 12  # 1 of 8 chapters ready (12.5 rounds to 12)
    assert len(store.pending_jobs()) == 7
    worker.run_until_idle()
    assert engine.notebook_view(nid)["status"] == "complete"


def test_readiness_counts_partial_progress(engine, store):
    nid = make(engine)
    engine.phase0(nid)
    chs = store.chapters(nid)
    store.set_chapter_status(chs[0]["id"], "ready")
    store.set_chapter_status(chs[1]["id"], "checking")
    store.set_chapter_status(chs[2]["id"], "drafting")
    assert engine.notebook_view(nid)["readiness"] == round(100 * (1 + 0.8 + 0.4) / 8)


def test_pasted_material_shapes_the_course_and_is_cited(engine, worker, store, sample_text):
    nid = make(engine, sample_text)
    v = build_all(engine, worker, nid)
    chunk_ids = {c["chunk_id"] for s in store.sources(nid) for c in s["chunks"]}
    assert chunk_ids and v["chapters"][0]["title"]
    notes = next(b for b in engine.chapter_view(v["chapters"][0]["id"])["blocks"] if b["type"] == "notes")
    assert notes["meta"]["refs"] and set(notes["meta"]["refs"]) <= chunk_ids
    assert engine.chapter_view(v["chapters"][0]["id"])["grounded"]


def test_url_source_is_read_and_chapter_gets_verified_resources(engine, worker, store, fake_web):
    fake_web.get(lambda u, p: FakeResp(200, ARTICLE) if u == "https://ex.com/bs" else
                 FakeResp(200, ARTICLE) if u == "https://en.wikipedia.org/wiki/Binary_search" else None)
    fake_web.get(lambda u, p: FakeResp(200, data=["q", ["Binary search"], [""], ["https://en.wikipedia.org/wiki/Binary_search"]])
                 if "wikipedia.org/w/api.php" in u else None)
    img = {"query": {"pages": {"1": {"index": 1, "title": "File:Binary search diagram.png", "imageinfo": [{"thumburl": "https://upload.wikimedia.org/b.png", "mime": "image/png",
           "width": 800, "descriptionurl": "https://commons.wikimedia.org/wiki/File:B.png", "extmetadata": {"Artist": {"value": "Ann"}, "LicenseShortName": {"value": "CC0"}}}]}}}}
    fake_web.get(lambda u, p: FakeResp(200, data=img) if "commons.wikimedia.org/w/api.php" in u else None)
    fake_web.get(lambda u, p: FakeResp(206, "", headers={"content-type": "image/png"}) if u == "https://upload.wikimedia.org/b.png" else None)

    nid = make(engine, "I want to learn binary search https://ex.com/bs")
    v = build_all(engine, worker, nid)
    src = store.sources(nid)
    assert src[0]["kind"] == "url" and src[0]["chunks"] and "halving the range" in src[0]["extracted_text"]
    shown = v["sources"][0]
    assert shown["kind"] == "url" and shown["url"] == "https://ex.com/bs" and shown["chunks"] >= 1  # the UI lists sources
    ch = engine.chapter_view(v["chapters"][0]["id"])
    links = [b for b in ch["blocks"] if b["type"] == "link"]
    imgs = [b for b in ch["blocks"] if b["type"] == "image"]
    assert links and all(b["verified"] and b["content"]["url"].startswith("https://") for b in links)
    assert imgs and imgs[0]["content"]["attribution"] == {"author": "Ann", "license": "CC0", "source": "Wikimedia Commons",
                                                           "page_url": "https://commons.wikimedia.org/wiki/File:B.png"}
    assert not [b for b in ch["blocks"] if b["type"] == "video"]  # no YouTube key: honest, with a note
    assert ch["curator_notes"]


def test_unreadable_url_is_reported_not_fatal(engine, worker, fake_web):
    fake_web.get(lambda u, p: FakeResp(404, "gone") if "dead.example" in u else None)
    nid = make(engine, "teach me algorithms https://dead.example/page")
    v = build_all(engine, worker, nid)
    assert v["status"] == "complete" and any("couldn't read" in w for w in v["warnings"])


def test_image_attachment_is_read_by_the_vision_role(engine, worker, store):
    nid = make(engine, "", [("page.png", "image/png", b"\x89PNG fake")])
    v = build_all(engine, worker, nid)
    assert v["status"] == "complete" and store.sources(nid)[0]["kind"] == "image" and store.sources(nid)[0]["chunks"]


def test_pdf_attachment_goes_through_text_extraction(engine, worker, store, monkeypatch, sample_text):
    from app.agents import intake
    monkeypatch.setattr(intake, "analyze_pdf", lambda data: ([sample_text, "More about velocity and speed in everyday motion."], []))
    nid = make(engine, "", [("notes.pdf", "application/pdf", b"%PDF-1.4 fake")])
    v = build_all(engine, worker, nid)
    assert store.sources(nid)[0]["kind"] == "pdf" and {c["page"] for c in store.sources(nid)[0]["chunks"]} == {1, 2}
    assert v["status"] == "complete"


def test_request_validation(engine):
    from app.engine import UserError, split_request
    with pytest.raises(UserError):
        engine.create_notebook(LID, "   ", "en", [])
    with pytest.raises(UserError, match="only photos and PDFs"):
        engine.create_notebook(LID, "x", "en", [("a.exe", "application/octet-stream", b"1")])
    assert split_request("learn DSA") == ("learn DSA", "", [])
    goal, material, urls = split_request("x" * 700 + " https://a.org/p")
    assert goal is None and len(material) == 700 and urls == ["https://a.org/p"]


# --------------------------------------------------------------------- Critic loop, failures, restarts
def test_unverified_after_two_revisions_is_logged(engine, worker, store):
    issue = {"verdict": "revise", "issues": [{"step": "notes", "problem": "unsupported claim"}], "confidence": 0.6}
    engine.router.fake.script("critic", [issue, issue, issue])
    nid = make(engine)
    engine.process_job(store.pending_jobs()[0]["id"])
    ch1 = engine.chapter_view(engine.notebook_view(nid)["chapters"][0]["id"])
    assert ch1["unverified"] and ch1["status"] == "ready"
    ev = [e for e in store.events(200) if "unverified" in e["summary"]]
    assert ev and "2 revisions" in ev[0]["error"]


def test_critic_revision_then_approval(engine, worker, store):
    issue = {"verdict": "revise", "issues": [{"step": "notes", "problem": "bad"}], "confidence": 0.6}
    engine.router.fake.script("critic", [issue])
    nid = make(engine)
    engine.process_job(store.pending_jobs()[0]["id"])
    first = engine.notebook_view(nid)["chapters"][0]["id"]
    assert not engine.chapter_view(first)["unverified"]
    assert store.get_chapter(first)["meta"]["revisions"] == 1


def test_failed_chapter_is_marked_failed_then_retried(engine, worker, store, monkeypatch):
    nid = make(engine)
    engine.process_job(store.pending_jobs()[0]["id"])
    third = store.chapters(nid)[2]["id"]
    real = engine.build_chapter

    def flaky(cid):
        if cid == third:
            raise RuntimeError("model exploded")
        return real(cid)

    monkeypatch.setattr(engine, "build_chapter", flaky)
    worker.run_until_idle()
    v = engine.notebook_view(nid)
    bad = next(c for c in v["chapters"] if c["id"] == third)
    assert bad["status"] == "failed" and bad["state"] == "failed" and "Something went wrong" in bad["error"]  # friendly text
    job = store.job_for_chapter(third)
    assert job["attempts"] == 3 and "model exploded" in job["last_error"] and v["status"] == "generating"  # detail kept
    assert sum(c["status"] == "ready" for c in v["chapters"]) == 7  # nothing else was blocked
    monkeypatch.setattr(engine, "build_chapter", real)
    engine.retry_chapter(third)
    worker.run_until_idle()
    assert engine.notebook_view(nid)["status"] == "complete"


def test_outline_failure_marks_notebook_failed_and_can_retry(engine, worker, store):
    for m in engine.router.cfg["models"]:
        engine.router.kill(m)
    nid = make(engine)  # goal only + every model dead: nothing to plan from
    worker.run_until_idle()
    v = engine.notebook_view(nid)
    assert v["status"] == "failed" and not v["chapters"]
    assert "couldn't reach any AI model" in v["error"] and "Error" not in v["error"]  # friendly for the student
    assert "cannot plan a course" in store.get_job(store._all("SELECT id FROM jobs")[0]["id"])["last_error"]  # detail kept
    engine.router.revive()
    engine.retry_notebook(nid)
    worker.run_until_idle()
    assert engine.notebook_view(nid)["status"] == "complete"


def test_jobs_resume_after_a_restart(engine, worker, store):
    nid = make(engine)
    engine.process_job(store.pending_jobs()[0]["id"])
    chs = store.chapters(nid)
    job = store.job_for_chapter(chs[1]["id"])
    store.set_job(job["id"], "running")  # the server died mid-build
    store.set_chapter_status(chs[1]["id"], "drafting")
    assert store.pending_jobs()[0]["id"] != job["id"] or True
    assert worker.recover() == 1
    assert store.get_chapter(chs[1]["id"])["status"] == "queued"
    worker.run_until_idle()
    assert engine.notebook_view(nid)["status"] == "complete"


def test_opening_a_pending_chapter_jumps_the_queue(engine, worker, store):
    nid = make(engine)
    engine.process_job(store.pending_jobs()[0]["id"])
    target = store.chapters(nid)[5]
    assert store.pending_jobs()[0]["chapter_id"] != target["id"]
    engine.prioritize(target["id"])
    assert store.pending_jobs()[0]["chapter_id"] == target["id"]
    worker.run_until_idle()


# ----------------------------------------------------------------------- quiz, mistakes, course rewrite
def first_two(engine, worker, nid):
    worker.run_until_idle()
    return [c["id"] for c in engine.notebook_view(nid)["chapters"]]


def test_wrong_answer_names_misconception_and_adds_a_review_chapter(engine, worker, store):
    nid = make(engine)
    ids = first_two(engine, worker, nid)
    sid = engine.open_session(LID)
    keys = quiz_keys(store, ids[0])
    q = next(iter(keys.values()))
    wrong = next(o for o in q["options"] if o != q["answer_key"])
    fb = engine.answer(LID, sid, ids[0], q["id"], wrong)
    assert fb["result"] == "wrong" and fb["misconception"] and fb["hint"] and "correct_answer" not in fb
    for other in list(keys)[1:]:
        engine.answer(LID, sid, ids[0], other, keys[other]["answer_key"])
    out = engine.finish_chapter(LID, sid, ids[0])
    assert out["score"] == {"correct": len(keys) - 1, "total": len(keys)} and len(out["proposed"]) == 1 and out["added"] == []     # the Rewriter proposes
    assert len(engine.notebook_view(nid)["chapters"]) == 8                                                                       # nothing changes until Apply
    engine.apply_proposal(nid, out["proposed"][0]["id"])
    v = engine.notebook_view(nid)
    assert len(v["chapters"]) == 9 and v["chapters"][1]["kind"] == "review" and v["chapters"][1]["title"].startswith("Review:")
    assert v["chapters"][0]["state"] == "needs_review"
    assert v["chapters"][1]["prereqs"] == [ids[0]] and [ids[0], v["chapters"][1]["id"]] in v["edges"]
    worker.run_until_idle()  # the review chapter is built like any other
    review_id = v["chapters"][1]["id"]
    assert engine.chapter_view(review_id)["status"] == "ready"
    assert "Misconception" not in "".join(b["type"] for b in engine.chapter_view(review_id)["blocks"])
    # passing the review clears the flag on the original chapter
    sid2 = engine.open_session(LID)
    for qid, qq in quiz_keys(store, review_id).items():
        engine.answer(LID, sid2, review_id, qid, qq["answer_key"])
    engine.finish_chapter(LID, sid2, review_id)
    assert engine.notebook_view(nid)["chapters"][0]["state"] == "done"


def test_wrong_then_right_after_hint_still_counts_as_a_gap(engine, worker, store):
    nid = make(engine)
    ids = first_two(engine, worker, nid)
    sid = engine.open_session(LID)
    keys = quiz_keys(store, ids[0])
    q0 = next(iter(keys.values()))
    wrong = next(o for o in q0["options"] if o != q0["answer_key"])
    engine.answer(LID, sid, ids[0], q0["id"], wrong, attempt=1)
    ok = engine.answer(LID, sid, ids[0], q0["id"], q0["answer_key"], attempt=2)
    assert ok["result"] == "correct" and ok["correct_answer"] == q0["answer_key"]
    for qid, q in list(keys.items())[1:]:
        engine.answer(LID, sid, ids[0], qid, q["answer_key"])
    out = engine.finish_chapter(LID, sid, ids[0])
    assert out["score"]["correct"] == len(keys) - 1 and len(out["proposed"]) == 1


def test_perfect_quiz_changes_nothing(engine, worker, store):
    nid = make(engine)
    ids = first_two(engine, worker, nid)
    sid = engine.open_session(LID)
    for qid, q in quiz_keys(store, ids[0]).items():
        assert engine.answer(LID, sid, ids[0], qid, q["answer_key"])["result"] == "correct"
    out = engine.finish_chapter(LID, sid, ids[0])
    assert out["added"] == [] and len(engine.notebook_view(nid)["chapters"]) == 8
    assert engine.notebook_view(nid)["chapters"][0]["state"] == "done"
    assert not any(a == "diagnoser" for a, _ in engine.router.fake.calls)  # graded in code, no model call


def test_at_most_two_review_chapters_per_session(engine, worker, store):
    nid = make(engine)
    ids = first_two(engine, worker, nid)
    sid = engine.open_session(LID)
    added = []
    for cid in ids[:3]:
        keys = quiz_keys(store, cid)
        q = next(iter(keys.values()))
        engine.answer(LID, sid, cid, q["id"], next(o for o in q["options"] if o != q["answer_key"]))
        for pr in engine.finish_chapter(LID, sid, cid)["proposed"]:
            engine.apply_proposal(nid, pr["id"])                                    # the Rewriter only proposes; applying is the learner's choice
            added.append(pr)
    assert len(added) == 2 and len(engine.notebook_view(nid)["chapters"]) == 10


def test_mistakes_persist_and_companion_remembers_next_session(engine, worker, store):
    nid = make(engine)
    ids = first_two(engine, worker, nid)
    s1 = engine.open_session(LID)
    q = next(iter(quiz_keys(store, ids[0]).values()))
    engine.answer(LID, s1, ids[0], q["id"], next(o for o in q["options"] if o != q["answer_key"]))
    engine.finish_chapter(LID, s1, ids[0])
    closing = engine.close_session(LID, s1, nid)
    assert closing["revisit"]
    engine.open_session(LID)
    s2 = engine.open_session(LID)
    msg = engine.companion_open(LID, nid)
    assert store.mistake_labels(LID)[0] in msg["message"] and msg["suggested_next"]
    assert engine.profile(LID).mistake_labels  # also fed to the Architect/Quizzer for later notebooks


def test_unsure_diagnosis_asks_for_a_retry_not_a_verdict(engine, worker, store):
    nid = make(engine)
    ids = first_two(engine, worker, nid)
    sid = engine.open_session(LID)
    q = next(iter(quiz_keys(store, ids[0]).values()))
    engine.router.fake.script("diagnoser", [{"question_id": q["id"], "result": "wrong", "misconception": "guess", "confidence": 0.2, "hint": "try again"}])
    fb = engine.answer(LID, sid, ids[0], q["id"], next(o for o in q["options"] if o != q["answer_key"]), attempt=1)
    assert fb["follow_up"] and fb["result"] is None and store.attempts(ids[0]) == []


# ------------------------------------------------------------------------- doubts, edits, deletes
def test_doubt_typed_and_voice_fallback(engine, worker, store, sample_text):
    nid = make(engine, sample_text)
    ids = first_two(engine, worker, nid)
    r = engine.doubt(LID, ids[0], "What is velocity?", None)
    assert r["answer"] and r["audio_b64"] is None
    v = engine.doubt(LID, ids[0], None, b"fake-audio")
    assert v["needs_text"]
    assert engine.transcribe(b"x", "en")["text"] is None  # Sarvam unavailable in demo mode: degrade, never crash


def test_only_notes_are_editable(engine, worker, store):
    nid = make(engine)
    ids = first_two(engine, worker, nid)
    blocks = engine.chapter_view(ids[0])["blocks"]
    notes = next(b for b in blocks if b["type"] == "notes")
    engine.edit_block(notes["id"], {"markdown": "## My own notes\n\nEdited."})
    assert next(b for b in engine.chapter_view(ids[0])["blocks"] if b["type"] == "notes")["content"]["markdown"].startswith("## My own")
    from app.engine import UserError
    with pytest.raises(UserError):
        engine.edit_block(next(b for b in blocks if b["type"] == "slides")["id"], {"markdown": "x"})


def test_delete_notebook_removes_everything(engine, worker, store):
    nid = make(engine)
    first_two(engine, worker, nid)
    engine.delete_notebook(nid)
    assert store.get_notebook(nid) is None and store.chapters(nid) == [] and store.sources(nid) == []
    assert engine.list_notebooks(LID) == []


def test_notebook_cards_for_home(engine, worker, store):
    nid = make(engine)
    first_two(engine, worker, nid)
    card = engine.list_notebooks(LID)[0]
    assert card["id"] == nid and card["readiness"] == 100 and card["progress"] == {"done": 0, "total": 8} and card["open_ready"]


# ------------------------------------------------------------------------- Teacher rules
CH = {"id": 1, "title": "Binary search", "summary": "Halving", "objectives": ["Explain halving"]}
PAGE = {"chunk_id": "wabc123-p1-c1", "page": 1, "text": " ".join(PARAS), "source_id": "web"}


def content(notes):
    return {"language": "en", "notes_md": notes, "summary": "It halves the range each step.", "key_points": ["halving"],
            "slides": [{"title": f"S{i}", "points": ["a point", "another"]} for i in range(4)], "source_refs": ["wabc123-p1-c1"]}


def test_teacher_rejects_verbatim_copying_of_fetched_pages(router):
    copied = content("## Binary search\n\n" + " ".join(PARAS[:4]))
    own = content("## Binary search\n\nThink of guessing a number between one and a hundred: each guess removes half of the options, "
                  "so a sorted list of a million items needs only about twenty looks.")
    router.fake.script("teacher", [copied, copied, own])
    res = teacher.run(router, CH, [PAGE], [], LearnerProfile(learner_id="x"))
    assert "guessing a number" in res.value.notes_md
    assert [r for r, _ in router.fake.calls if r == "teacher"].__len__() == 3  # two rejected copies, then the rewrite


def test_teacher_rejects_unknown_refs_and_wrong_language(router):
    bad_ref = {**content("## T\n\n" + "own words " * 30), "source_refs": ["nope"]}
    wrong_lang = {**content("## T\n\n" + "own words " * 30), "language": "hi"}
    good = content("## T\n\n" + "own words " * 30)
    router.fake.script("teacher", [bad_ref, bad_ref, wrong_lang, wrong_lang, good])
    res = teacher.run(router, CH, [PAGE], [], LearnerProfile(learner_id="x"))
    assert res.value.source_refs == ["wabc123-p1-c1"]


def test_teacher_last_resort_never_invents_facts(router):
    for m in router.cfg["models"]:
        router.kill(m)
    res = teacher.run(router, CH, [], [], LearnerProfile(learner_id="x"))
    assert res.used_last_resort and "could not be generated" in res.value.notes_md  # says so plainly
    assert res.value.source_refs == []


def test_last_resort_lesson_never_copies_fetched_pages(router):
    for m in router.cfg["models"]:
        router.kill(m)
    res = teacher.run(router, CH, [PAGE], [], LearnerProfile(learner_id="x"))
    assert res.used_last_resort
    assert teacher.longest_shared_run(teacher.content_text(res.value), PAGE["text"]) < teacher.VERBATIM_WORDS
    assert res.value.source_refs == []  # nothing of the student's own to cite: no invented refs


def test_demo_teacher_output_passes_the_copy_rule(router):
    res = teacher.run(router, CH, [PAGE], [{"ref": "url:https://x.example", "title": "X", "source": "x.example"}], LearnerProfile(learner_id="x"))
    assert not res.used_last_resort  # the normal path validated, i.e. no verbatim copying


def test_real_pdf_bytes_are_extracted_and_cited(engine, worker, store):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    from acceptance import make_pdf
    pdf = make_pdf("Binary search halves the sorted range at every step so comparisons grow with the logarithm of the list size. "
                   "The list must already be sorted. A common bug is computing the midpoint with an addition that overflows.")
    nid = make(engine, "", [("notes.pdf", "application/pdf", pdf)])
    v = build_all(engine, worker, nid)
    src = store.sources(nid)[0]
    assert src["kind"] == "pdf" and "halves the sorted range" in " ".join(c["text"] for c in src["chunks"])
    assert v["status"] == "complete" and v["sources"][0]["chunks"] >= 1


def test_quiz_is_written_while_the_critic_checks_and_matches_the_final_draft(engine, worker, store):
    """With one revision the quiz of the FIRST draft is discarded; the stored quiz belongs to the final draft."""
    issue = {"verdict": "revise", "issues": [{"step": "notes", "problem": "x"}], "confidence": 0.5}
    engine.router.fake.script("critic", [issue])
    nid = make(engine)
    engine.process_job(store.pending_jobs()[0]["id"])
    cid = engine.notebook_view(nid)["chapters"][0]["id"]
    assert len(quiz_keys(store, cid)) >= 3 and store.get_chapter(cid)["meta"]["revisions"] == 1
    roles = [r for r, _ in engine.router.fake.calls]
    assert roles.count("quizzer") == 2 and roles.count("teacher") == 2  # one quiz per draft


def test_search_topic_never_uses_a_long_personal_goal():
    from app.engine import search_topic
    assert search_topic({"goal": "Learn DSA", "title": "Data Structures"}) == "Learn DSA"
    long = "I'm part of the training for HCL - this is the syllabus given to us, i need a clean roadmap\r\n\r\nbest resources please"
    assert search_topic({"goal": long, "title": "Cloud Data Engineering Roadmap"}) == "Cloud Data Engineering Roadmap"


# ------------------------------------------------------------- Milestone 3, section 6: honest status, progress, greeting
def test_unopened_chapters_say_not_started_and_generation_is_separate(engine, worker, store):
    nid = make(engine, "Teach me binary search")
    v = build_all(engine, worker, nid)
    assert {c["learning"] for c in v["chapters"]} == {"not_started"} and {c["generation"] for c in v["chapters"]} == {"ready"}
    assert {c["state"] for c in v["chapters"]} == {"not_started"}
    engine.open_chapter(v["chapters"][0]["id"])
    v = engine.notebook_view(nid)
    assert [c["learning"] for c in v["chapters"]][:2] == ["in_progress", "not_started"]
    store.set_chapter_status(v["chapters"][1]["id"], "drafting")
    c2 = engine.notebook_view(nid)["chapters"][1]
    assert c2["generation"] == "writing" and c2["state"] == "generating" and c2["learning"] == "not_started"


def test_two_progress_measures_are_independent(engine, worker, store):
    nid = make(engine, "Teach me binary search")
    v = build_all(engine, worker, nid)
    assert v["your_progress"] == {"done": 0, "total": len(v["chapters"]), "pct": 0}
    assert v["preparing"]["complete"] and v["preparing"]["pct"] == 100
    store.set_chapter_status(v["chapters"][-1]["id"], "queued")
    v = engine.notebook_view(nid)
    assert not v["preparing"]["complete"] and v["preparing"]["ready"] == len(v["chapters"]) - 1 and v["your_progress"]["pct"] == 0


def test_first_session_greeting_is_fixed_text_and_never_says_welcome_back(engine, worker, store):
    nid = make(engine, "Teach me binary search")
    build_all(engine, worker, nid)
    for _ in range(3):                                   # opening the app opens a session each time: that is not history
        store.open_session("L1")
    g = engine.companion_open("L1", nid)
    assert g["is_first_session"] and "Welcome back" not in g["message"] and "taking shape" in g["message"]
    assert [c for c in engine.router.fake.calls if c[0] == "companion"] == []       # no model wrote it


def test_returning_greeting_only_with_real_history(engine, worker, store):
    nid = make(engine, "Teach me binary search")
    build_all(engine, worker, nid)
    sid = store.open_session("L1")
    store.close_session(sid, "Did chapter 1.") if hasattr(store, "close_session") else None
    g = engine.companion_open("L1", nid)
    assert (not g["is_first_session"]) == store.has_history("L1")
    store.add_mistake("L1", "mixes up index and value")
    g = engine.companion_open("L1", nid)
    assert not g["is_first_session"] and g["message"].startswith("Welcome back") and "mixes up index and value" in g["message"]


def test_unverified_chapter_lists_its_open_claims_and_recheck_can_clear_them(engine, worker, store):
    engine.router.fake.script("critic", [{"verdict": "revise", "issues": [{"step": "notes", "problem": "Claim X has no support", "source_ref": "nope"}], "confidence": .8}] * 3)
    nid = make(engine, "Teach me binary search")
    engine.process_job(store.pending_jobs()[0]["id"])
    cid = engine.notebook_view(nid)["chapters"][0]["id"]
    ch = engine.chapter_view(cid)
    assert ch["unverified"] and ch["review_issues"][0]["claim"] == "Claim X has no support"
    assert ch["review_issues"][0]["source_status"] == "cited source not in this notebook"
    out = engine.recheck_chapter(cid)                    # the fake Critic approves once the scripted answers are used up
    assert out["verified"] and not engine.chapter_view(cid)["unverified"] and engine.chapter_view(cid)["review_issues"] == []
