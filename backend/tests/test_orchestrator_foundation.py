"""Milestone 4 foundation: board, bus, manifests, referee, decision emitter. Plain code: no model is ever called here."""
import ast
import threading
from pathlib import Path

import pytest
import yaml

from app.db import Store
from app.orchestrator import (REFEREE, Board, BoardError, Bus, DecisionEmitter, LoopStopped, Referee, StaleTransition, load_manifests, orchestrator_mode)
from app.orchestrator.manifests import ManifestError, parse

ROOT = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def parts(clock):
    store = Store(":memory:")
    reg = load_manifests()
    board, bus, emitter = Board(store, reg, clock), Bus(store, reg, clock), DecisionEmitter(store, clock)
    return store, board, bus, emitter, Referee(board, bus, emitter, reg)


# ------------------------------------------------------------------------------------------------ manifests
def test_manifests_load_and_match_the_model_roles():
    roles = set(yaml.safe_load((ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))["roles"])
    reg = load_manifests(known_roles=roles)
    assert {"intake", "architect", "curator", "teacher", "critic", "quizzer", "illustrator", "diagnoser", "rewriter", "companion", "doubt", "editor", "scoper", "practice", "slides"} <= set(reg.agents)
    assert reg.release_gate("proposal") == "proposal" and reg.release_gate("notes") == "verified"


@pytest.mark.parametrize("raw, fragment", [
    ({"agents": {"a": {"layer": 1, "model_role": "x", "needs": ["nope"]}}}, "unknown artifact type"),
    ({"agents": {"a": {"layer": 1, "model_role": "x", "may_request": ["ghost"]}}}, "unknown agent"),
    ({"agents": {"a": {"layer": 1, "model_role": "x", "release_gate": "maybe"}}}, "release_gate"),
    ({"agents": {"a": {"layer": 1, "model_role": "x", "produces": ["notes"]}, "b": {"layer": 1, "model_role": "x", "produces": ["notes"]}}}, "several producers"),
    ({"agents": {"a": {"layer": 1, "model_role": "x", "colour": "red"}}}, "colour"),
])
def test_bad_manifests_are_refused(raw, fragment):
    with pytest.raises(ManifestError, match=fragment):
        parse(raw)


def test_orchestrator_imports_no_router_and_no_model_client():
    banned = ("router", "providers", "engine", "agents", "httpx", "tools")
    for f in (ROOT / "backend" / "app" / "orchestrator").glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else ([node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for m in mods:
                parts = m.split(".")
                assert not set(parts) & set(banned) and not (isinstance(node, ast.ImportFrom) and node.level > 1), f"{f.name} imports {m}"


# ------------------------------------------------------------------------------------------------ board
def test_dependencies_unblock_in_order_and_only_the_referee_releases(parts):
    store, board, bus, emitter, ref = parts
    a = board.add("nb", "outline", owner_agent="architect")
    b = board.add("nb", "notes", chapter_id=1, owner_agent="teacher", depends_on=[a])
    assert board.get(a)["status"] == "ready" and board.get(b)["status"] == "blocked"
    task = board.claim("architect")
    assert task["id"] == a and board.get(a)["status"] == "claimed"
    assert board.claim("teacher") is None                                  # still blocked
    board.transition(a, "drafted", "architect")
    with pytest.raises(PermissionError):
        board.transition(a, "released", "architect")                         # agents never release
    with pytest.raises(PermissionError):
        board.transition(a, "approved", "architect")                         # a drafted artifact cannot approve itself
    board.transition(a, "checking", "critic")
    board.transition(a, "approved", "critic")
    assert board.refresh_ready("nb") == [b]
    assert board.claim("teacher")["id"] == b


def test_transitions_are_validated_and_idempotent(parts):
    _, board, *_ = parts
    a = board.add("nb", "notes", owner_agent="teacher")
    board.claim("teacher")
    assert board.transition(a, "drafted", "teacher") is True
    assert board.transition(a, "drafted", "teacher") is False                # same status again: no-op, no error
    with pytest.raises(BoardError):
        board.transition(a, "claimed", REFEREE)                              # drafted -> claimed is not a legal move
    with pytest.raises(StaleTransition):
        board.transition(a, "checking", "critic", expect="claimed")
    board.add("nb", "notes", owner_agent="teacher")
    mine = board.claim("teacher")["id"]
    with pytest.raises(PermissionError):
        board.transition(mine, "drafted", "quizzer")                          # claimed by the teacher, not the quizzer


def test_priority_and_concurrency_cap(parts):
    _, board, *_ = parts
    low = board.add("nb", "notes", owner_agent="teacher", priority=60)
    high = board.add("nb", "notes", owner_agent="teacher", priority=0)
    board.add("nb", "notes", owner_agent="teacher", priority=30)
    assert board.claim("teacher")["id"] == high
    assert board.claim("teacher")["priority"] == 30                           # concurrency 2 for the teacher
    assert board.claim("teacher") is None and board.get(low)["status"] == "ready"


def test_claims_are_atomic_across_connections(tmp_path):
    path = tmp_path / "board.db"
    reg = load_manifests()
    seed = Store(path)
    board0 = Board(seed, reg)
    ids = [board0.add("nb", "video", owner_agent="curator") for _ in range(30)]
    won, errors = [], []

    def worker(i):
        try:
            b = Board(Store(path), reg)                                      # its own connection, like a second process
            while True:
                t = b.claim("curator", lease_s=60)
                if not t:
                    break
                won.append(t["id"])
                b.transition(t["id"], "drafted", "curator")
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and sorted(won) == sorted(ids)                          # every task claimed exactly once, no lock errors


def test_expired_lease_goes_back_to_ready_then_fails_when_retries_are_spent(parts, clock):
    _, board, *_ = parts
    a = board.add("nb", "outline", owner_agent="architect")                   # architect: max_retries 1
    board.claim("architect", lease_s=30)
    clock.t += 31
    assert [r["to"] for r in board.reap_expired()] == ["ready"]
    board.claim("architect", lease_s=30)
    clock.t += 31
    assert [r["to"] for r in board.reap_expired()] == ["failed"] and board.get(a)["status"] == "failed"


def test_heartbeat_keeps_a_claim_and_restart_resumes_work(tmp_path, clock):
    path = tmp_path / "r.db"
    reg = load_manifests()
    board = Board(Store(path), reg, clock)
    a = board.add("nb", "video", owner_agent="curator")
    board.claim("curator", lease_s=30)
    clock.t += 20
    assert board.heartbeat(a, "curator", 30) and not board.reap_expired()
    reopened = Board(Store(path), reg, clock)                                  # the server restarted
    assert reopened.recover_after_restart() == 1
    assert reopened.get(a)["status"] == "ready" and reopened.claim("curator")["id"] == a


# ------------------------------------------------------------------------------------------------ bus
def test_messages_are_delivered_once_and_follow_the_manifest(parts):
    _, _, bus, *_ = parts
    m = bus.send("nb", "critic", "teacher", "Fixes", payload={"claim": 7})
    assert [x["id"] for x in bus.receive("teacher")] == [m["id"]]
    assert bus.receive("teacher") == []                                       # delivered once
    with pytest.raises(PermissionError):
        bus.send("nb", "quizzer", "curator", "NeedHelp")                      # quizzer may only ask the teacher
    with pytest.raises(PermissionError):
        bus.send("nb", "teacher", "critic", "Release")                        # only the referee releases
    with pytest.raises(BoardError):
        bus.send("nb", "critic", "teacher", "Gossip")


def test_loop_limit_ttl_and_dedupe(parts, clock):
    _, _, bus, _, ref = parts
    first = bus.send("nb", "critic", "teacher", "Fixes", correlation_id="c1", payload={"n": 1})
    again = bus.send("nb", "critic", "teacher", "Fixes", correlation_id="c9", payload={"n": 1})
    assert again["deduped"] and again["id"] == first["id"]                    # identical open request is not stored twice
    bus.send("nb", "teacher", "critic", "ArtifactReady", correlation_id="c1", payload={"n": 2})
    bus.send("nb", "critic", "teacher", "Fixes", correlation_id="c1", payload={"n": 3})
    with pytest.raises(LoopStopped):
        bus.send("nb", "teacher", "critic", "ArtifactReady", correlation_id="c1", payload={"n": 4})   # the 4th message in one chain
    assert ref.send("nb", "teacher", "critic", "ArtifactReady", correlation_id="c1", payload={"n": 5}) is None    # the referee stops it quietly...
    assert any(d["reason_code"] == "loop_limit" for d in ref.emit.recent("nb"))                              # ...and records why
    old = bus.send("nb", "critic", "teacher", "Clarify", ttl_s=10, payload={"q": 1})
    clock.t += 11
    assert old["id"] not in [m["id"] for m in bus.receive("teacher")]          # expired before anyone read it


def test_batched_sends_commit_together(parts):
    _, _, bus, *_ = parts
    got = bus.send_many([dict(notebook_id="nb", frm="critic", to="teacher", type="Fixes", payload={"i": i}) for i in range(5)])
    assert len(got) == 5 and bus.pending("teacher") == 5


# ------------------------------------------------------------------------------------------------ referee
def test_release_order_outline_first_slides_after_notes_proposals_never(parts):
    store, board, bus, emitter, ref = parts

    def make(type, status, ch=None):
        a = board.add("nb", type, chapter_id=ch, owner_agent="x")
        with store.transaction(immediate=True):
            store.conn.execute("UPDATE artifacts SET status=? WHERE id=?", (status, a))
        return a
    notes = make("notes", "drafted", 1)
    quiz = make("quiz", "approved", 1)
    slides = make("slide_deck", "approved", 1)
    prop = make("proposal", "approved")
    assert ref.release_pending("nb") == []                                     # no outline yet: nothing is shown
    outline = make("outline", "approved")
    got = [a["type"] for a in ref.release_pending("nb")]
    assert got == ["outline", "notes", "quiz", "slide_deck"] and board.get(prop)["status"] == "approved"
    assert board.get(notes)["flags"]["draft"] is True                          # notes appear as a labelled draft first
    ref.upgrade_released(notes, {"md": "fixed"}, verified=True)
    a = board.get(notes)
    assert a["version"] == 2 and a["flags"]["draft"] is False and a["flags"]["verified"] is True and a["payload"] == {"md": "fixed"}
    assert board.get(outline)["status"] == "released" and board.get(slides)["status"] == "released"


def test_slides_wait_for_the_notes(parts):
    store, board, _, _, ref = parts
    ids = {}
    for t, ch in (("outline", None), ("slide_deck", 2)):
        ids[t] = board.add("nb", t, chapter_id=ch, owner_agent="x")
        with store.transaction(immediate=True):
            store.conn.execute("UPDATE artifacts SET status='approved' WHERE id=?", (ids[t],))
    assert [a["type"] for a in ref.release_pending("nb")] == ["outline"]       # no notes released for chapter 2 yet


def test_critic_loop_stops_after_two_revisions_and_ships_unverified(parts):
    _, board, _, emitter, ref = parts
    a = board.add("nb", "notes", chapter_id=1, owner_agent="teacher")
    assert ref.allow_revision(a) and ref.allow_revision(a) and not ref.allow_revision(a)
    assert board.get(a)["flags"]["unverified"] is True and board.get(a)["flags"]["revisions"] == 2
    assert [d["kind"] for d in emitter.recent("nb")] == ["stopped"]


def test_priority_tags_and_opening_a_chapter_moves_it_up(parts):
    _, board, _, emitter, ref = parts
    assert Referee.priority(5, opened=True) < Referee.priority(1) < Referee.priority(2) < Referee.priority(6) < Referee.priority(3, prefetch=True) < Referee.priority(1, background=True)
    far = board.add("nb", "notes", chapter_id=9, owner_agent="teacher", priority=Referee.priority(9))
    assert ref.bump_opened("nb", 9) == 1 and board.get(far)["priority"] == 0
    assert "you opened it" in emitter.recent("nb")[-1]["reason_text"]


def test_stuck_work_and_deadlocks_fail_with_a_reason(parts):
    _, board, _, emitter, ref = parts
    dead = board.add("nb", "outline", owner_agent="architect")
    child = board.add("nb", "notes", chapter_id=1, owner_agent="teacher", depends_on=[dead])
    board.claim("architect")
    board.transition(dead, "failed", "architect")
    assert ref.detect_stuck("nb") == [child] and board.get(child)["status"] == "failed"
    with pytest.raises(BoardError, match="do not exist"):
        board.add("nb2", "notes", owner_agent="teacher", depends_on=[999])                  # a dangling dependency fails loudly instead of blocking forever
    x, y = board.add("nb2", "notes", owner_agent="teacher"), board.add("nb2", "quiz", owner_agent="quizzer")
    store = board.store
    with store.transaction(immediate=True):                                                  # a dependency loop: nothing can ever run
        store.conn.execute("UPDATE artifacts SET status='blocked', depends_on=? WHERE id=?", (f"[{y}]", x))
        store.conn.execute("UPDATE artifacts SET status='blocked', depends_on=? WHERE id=?", (f"[{x}]", y))
    assert sorted(ref.detect_stuck("nb2")) == sorted([x, y])
    assert all(d["severity"] == "warning" for d in emitter.recent("nb"))


def test_kill_switch_and_one_time_fallback(parts):
    _, _, _, emitter, ref = parts
    assert orchestrator_mode({}) == "off" and orchestrator_mode({"AASAAN_ORCHESTRATOR": "OFF"}) == "off"
    assert orchestrator_mode({"AASAAN_ORCHESTRATOR": "on"}) == "on" and orchestrator_mode({"AASAAN_ORCHESTRATOR": "per-notebook"}) == "per-notebook"
    assert ref.enabled("nb", {"AASAAN_ORCHESTRATOR": "on"}) and not ref.enabled("nb", {"AASAAN_ORCHESTRATOR": "off"})
    env = {"AASAAN_ORCHESTRATOR": "per-notebook"}
    assert not ref.enabled("nb", env)
    ref.opt_in("nb")
    assert ref.enabled("nb", env) and not ref.enabled("other", env)
    assert ref.fallback_once("nb", 7, "build failed") is True and ref.fallback_once("nb", 7) is False   # once per chapter, and logged
    assert [d["kind"] for d in emitter.recent("nb")] == ["fell_back"]


def test_budget_blocks_further_calls_and_says_so(parts):
    store, board, bus, emitter, _ = parts
    from app.orchestrator import Budgets
    ref = Referee(board, bus, emitter, load_manifests(), Budgets(per_notebook=2, total=10))
    assert ref.charge("nb") and ref.charge("nb") and not ref.charge("nb") and not ref.charge("nb")
    assert len([d for d in emitter.recent("nb") if d["reason_code"] == "rate_limited"]) == 1    # reported once, not per call


def test_tick_recovers_releases_and_fails_in_one_pass(parts, clock):
    store, board, _, _, ref = parts
    out = board.add("nb", "outline", owner_agent="architect")
    board.claim("architect", lease_s=10)
    clock.t += 11
    assert ref.tick("nb")["reaped"] == 1 and board.get(out)["status"] == "ready"


# ------------------------------------------------------------------------------------------------ decisions
def test_decision_sentences_come_from_templates_and_are_scrubbed(parts):
    _, _, _, emitter, _ = parts
    r = emitter.emit("nb", "curator", "rejected", what="a video about graphs", reason_code="not_embeddable", counts={"checked": 14, "kept": 5})
    assert r["summary"] == "Curator rejected a video about graphs: it cannot be embedded (checked 14, kept 5)."
    leak = emitter.emit("nb", "teacher", "fixed", what="claim 7", reason_text="see C:\\Users\\me\\key.txt and sk-abcdef1234567890 system prompt: be nice")
    assert "C:\\" not in leak["summary"] + leak["reason_text"] and "sk-" not in leak["reason_text"] and "be nice" not in leak["reason_text"]
    assert len(emitter.emit("nb", "critic", "chose", what="x", reason_text=" ".join(["word"] * 40))["reason_text"].split()) <= 12
    assert emitter.emit("nb", "critic", "chose", what="y", reason_code="made_up")["reason_code"] == "other"
    with pytest.raises(ValueError):
        emitter.emit("nb", "critic", "pondered", what="z")


def test_one_record_per_decision_not_per_retry(parts):
    _, _, _, emitter, _ = parts
    a = emitter.emit("nb", "critic", "rejected", what="claim 3", reason_code="unsupported_claim", correlation_id="k1", artifact_id=5)
    b = emitter.emit("nb", "critic", "rejected", what="claim 3", reason_code="unsupported_claim", correlation_id="k1", artifact_id=5)
    assert a and b is None and len(emitter.recent("nb")) == 1


def test_three_notebooks_work_at_once_without_lock_errors_or_mixed_feeds(tmp_path):
    path = tmp_path / "multi.db"
    reg = load_manifests()
    errors = []

    def run(nb):
        try:
            store = Store(path)
            board, bus, em = Board(store, reg), Bus(store, reg), DecisionEmitter(store)
            ref = Referee(board, bus, em, reg)
            for i in range(15):
                a = board.add(nb, "outline" if i == 0 else "notes", chapter_id=i, owner_agent="architect" if i == 0 else "teacher")
                t = board.claim("architect" if i == 0 else "teacher", notebook_id=nb)
                board.transition(t["id"], "drafted", t["claimed_by"])
                em.emit(nb, "teacher", "planned", what=f"chapter {i}", correlation_id=f"{nb}-{i}", artifact_id=a)
                bus.send(nb, "critic", "teacher", "Fixes", correlation_id=f"{nb}-m{i}", payload={"i": i})
                ref.tick(nb)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{nb}: {e!r}")
    threads = [threading.Thread(target=run, args=(f"nb{i}",)) for i in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    em = DecisionEmitter(Store(path))
    for i in range(3):
        recs = em.recent(f"nb{i}")
        assert recs and all(r["notebook_id"] == f"nb{i}" for r in recs)
        assert len([r for r in recs if r["kind"] == "planned"]) == 15


def test_deleting_a_notebook_removes_its_board_messages_and_decisions(parts):
    store, board, bus, emitter, _ = parts
    nid = store.create_notebook("L", "goal", "en")
    board.add(nid, "outline", owner_agent="architect")
    bus.send(nid, "critic", "teacher", "Fixes")
    emitter.emit(nid, "critic", "planned", what="x")
    store.delete_notebook(nid)
    assert not board.list(nid) and bus.pending() == 0 and not emitter.recent(nid)


# ------------------------------------------------------------------------------------------------ review fixes (PR #6)
def test_a_message_is_received_by_exactly_one_reader_across_connections(tmp_path):
    path = tmp_path / "rx.db"
    reg = load_manifests()
    Bus(Store(path), reg).send_many([dict(notebook_id="nb", frm="critic", to="teacher", type="Fixes", correlation_id=f"c{i}", payload={"i": i}) for i in range(40)])
    got, errors = [], []

    def reader():
        try:
            b = Bus(Store(path), reg)
            while True:
                rows = b.receive("teacher", limit=3)
                if not rows:
                    break
                got.extend(m["id"] for m in rows)
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))
    ts = [threading.Thread(target=reader) for _ in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and len(got) == 40 and len(set(got)) == 40          # nobody got a message twice


def test_a_restart_refunds_the_interrupted_attempt(tmp_path, clock):
    path = tmp_path / "rf.db"
    reg = load_manifests()
    board = Board(Store(path), reg, clock)
    a = board.add("nb", "outline", owner_agent="architect")
    board.claim("architect")
    board.reap_expired()                                                    # nothing expired yet
    assert Board(Store(path), reg, clock).recover_after_restart() == 1
    assert board.get(a)["status"] == "ready" and board.get(a)["attempts"] == 0     # the crash did not use up a retry


def test_detect_stuck_leaves_work_whose_dependencies_are_released(parts):
    store, board, _, _, ref = parts
    done = board.add("nb", "outline", owner_agent="architect")
    with store.transaction(immediate=True):
        store.conn.execute("UPDATE artifacts SET status='released' WHERE id=?", (done,))
    waiting = board.add("nb", "notes", chapter_id=1, owner_agent="teacher", depends_on=[done])
    with store.transaction(immediate=True):
        store.conn.execute("UPDATE artifacts SET status='blocked' WHERE id=?", (waiting,))   # the readiness pass has not run yet
    assert ref.detect_stuck("nb") == [] and board.get(waiting)["status"] == "ready"


def test_decision_text_keeps_the_words_user_prompt_but_drops_a_pasted_prompt(parts):
    _, _, _, emitter, _ = parts
    ok = emitter.emit("nb", "teacher", "fixed", what="the user prompt example", reason_text="clearer wording")
    assert "user prompt example" in ok["summary"] and ok["reason_text"] == "clearer wording"
    bad = emitter.emit("nb", "teacher", "fixed", what="x", reason_text="system prompt: you are a secret agent")
    assert "secret agent" not in bad["reason_text"]


def test_manifests_need_a_producer_and_must_not_loop():
    a = {"layer": 1, "model_role": "x"}
    with pytest.raises(ManifestError, match="no agent produces"):
        parse({"agents": {"a": {**a, "needs": ["outline"]}}})
    with pytest.raises(ManifestError, match="cycle"):
        parse({"agents": {"a": {**a, "needs": ["outline"], "produces": ["notes"]}, "b": {**a, "needs": ["notes"], "produces": ["outline"]}}})


def test_an_unknown_release_gate_is_treated_as_the_strictest():
    from app.orchestrator.manifests import Manifest, Registry
    reg = Registry({"a": Manifest("a", 1, "x", produces=("notes",), release_gate="weird"), "b": Manifest("b", 1, "x", produces=("quiz",), release_gate="verified")})
    assert reg.release_gate("notes") == "weird"                              # no crash, and it outranks the known gates


def test_flags_and_missing_artifacts_fail_cleanly(parts):
    store, board, _, _, ref = parts
    a = board.add("nb", "notes", owner_agent="teacher")
    with store.transaction(immediate=True):
        store.conn.execute("UPDATE artifacts SET flags='[1,2]' WHERE id=?", (a,))     # a list where a dict belongs
    assert board.get(a)["flags"] == {} and ref.allow_revision(a)
    with pytest.raises(BoardError):
        board.set_flag(999, x=1)
    with pytest.raises(BoardError):
        ref.allow_revision(999)


# ------------------------------------------------------------------------------------------------ review fixes (PR #14, second round)
def test_a_tick_only_reaps_its_own_notebook_and_callers_must_own_what_they_touch(parts, clock):
    store, board, bus, emitter, ref = parts
    a = board.add("nb", "outline", owner_agent="architect")
    b = board.add("other", "video", owner_agent="curator")
    board.claim("architect", lease_s=10, notebook_id="nb")
    board.claim("curator", lease_s=10, notebook_id="other")
    clock.t += 11
    assert ref.tick("nb")["reaped"] == 1 and board.get(b)["status"] == "claimed"            # the other notebook's expired claim is left for its own tick
    c = board.add("nb", "notes", owner_agent="teacher")
    board.claim("teacher", notebook_id="nb")
    with pytest.raises(PermissionError):
        board.revise(c, "quizzer", {"x": 1})                                               # not the claim holder
    with pytest.raises(PermissionError):
        board.patch_released(c, {"x": 1}, actor="teacher")                                # referee only


def test_a_crash_looping_task_still_runs_out_of_retries(parts, clock):
    store, board, *_ = parts
    a = board.add("nb", "outline", owner_agent="architect")
    for _ in range(5):                                                                       # the worker dies mid-task on every start
        board.claim("architect", lease_s=60)
        board.recover_after_restart()
    assert board.get(a)["attempts"] >= 3                                                      # refunds stopped after two, so the retry cap can bite


def test_a_bus_without_manifests_refuses_peer_messages_and_multiline_prompts_are_scrubbed(parts):
    from app.orchestrator import Bus
    store, _, _, emitter, _ = parts
    with pytest.raises(PermissionError, match="no agent manifests"):
        Bus(store).send("nb", "critic", "teacher", "Fixes")
    leak = emitter.emit("nb", "teacher", "fixed", what="x", reason_text="system prompt: line one\nline two secret")      # newlines are folded first, so the whole prompt goes
    assert "secret" not in leak["reason_text"]
