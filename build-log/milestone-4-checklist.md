# Milestone 4 checklist (layered, pull-based agents + live decision feed)

Workflow: stacked branches (m4/foundation -> m4/intake-spike -> ...), each PR based on the previous branch; a step over ~2000 lines is split into several PRs; merged one by one by the user. Claude commits locally; the user handles git auth, push and PRs. Each PR: `pytest -m ""` green, UI checked in Chrome, report (what changed, tests, unverified, how to check).
Baseline for comparison: `eval/baseline/old.json` (5 frozen topics, old path). Re-run as `--label orchestrated` after Layer 3.

## Before M4: open items
- [ ] UX changes the user wants first (branch `ux/polish`)
- [ ] Milestone 5 leftovers: Grainient (files missing), reduced-motion check, 390/768/1440 screenshots both themes
- [ ] 10 approved test removals blocked by auto-mode: user deletes them or allows the edit (list in chat; markers already done)
- [ ] Tag `pre-orchestrator` exists at a6414e0

## Bugs and features noted by the user (2026-10-18)
- [ ] **BUG chat-symbols:** the chat could not handle complex symbols (maths, Greek, arrows, sub/superscripts, code punctuation, mixed scripts). Reproduce, fix input handling, answer rendering and the model payload.
- [ ] **BUG chat-memory:** chat did not keep or use its own earlier responses (no persistence of the conversation as context), and could not look outside the notebook's resources. Fix: send recent turns with each question, keep a rolling summary, and let the doubt answerer fall back to a web lookup when the notebook has no answer (labelled as outside the sources).
- [ ] **FEATURE book-sources:** when the learner gives the base prompt (for example "source down books for ..."), find real books on the topic (open catalogues: Open Library, Project Gutenberg, OpenStax, publisher pages), add them as a Resource the chat agent can look up, and cite them in answers as numbered references [1], [2] that link to the exact page so the learner can read on.
- Planned PRs: `m4/chat-fixes` (the two bugs) right after Intake, and `m4/book-sources` after Architect/Curator (it builds on the Curator and the citation chips).

## Branch plan (each depends on the previous unless noted)
| # | Branch | Section | Scope |
|---|---|---|---|
| 1 | `m4/foundation` | 2 | artifacts + agent_messages tables, manifests, atomic claims (BEGIN IMMEDIATE), leases, referee (no router import), kill switch, `DecisionEmitter` + `decision_log` table (backend only), SQLite WAL/busy_timeout |
| 2 | `m4/intake-spike` | 3 | Indra spike on fixtures + report + adoption decision (Docker, not native Windows) |
| 3 | `m4/intake` | 3 | extraction ladder, `source_pack`, quality report, Local-OCR-only setting, fixtures + 10 acceptance tests |
| 4 | `m4/architect-curator` | 4 | outline + coverage map + validation, resource_pack, parallel run |
| 4b | `m4/chat-fixes` | bugs | chat symbols, chat memory and out-of-resource lookup (see above) |
| 4c | `m4/book-sources` | feature | books as a source, numbered [n] citations with links |
| 5 | `m4/activity-feed` | 8 | SSE `decision` events (batched, replay, resume), `/decisions` endpoint, Activity timeline, "How this was made" chips |
| 6 | `m4/content-notes` | 5.1, 5.2 | depth profiles, typed Teacher sections, readability layer, expansion pass |
| 7 | `m4/critic-fixes` | 5.4 | claim-level Fixes loop (max 2), draft -> "Verified N of M", in-place patch |
| 8 | `m4/quiz-mascot` | 5.6 | question types, marking, results, mascot states, `quiz_attempts`, mastery |
| 9 | `m4/illustrator` | 5.5 | AST high-level flowchart (Python), toggle, line highlight |
| 10 | `m4/slides` | 5.3 | slide plan + slides, similarity check, carousel |
| 11 | `m4/user-layer-a` | 6 | Diagnoser, Companion, Doubt answerer |
| 12 | `m4/user-layer-b` | 6 | Editor, Rewriter (proposals), Scoper, Practice Curator |
| 13 | `m4/hardening` | 2.4, 10 | chaos, 3-notebook concurrency, baseline compare, `AASAAN_TRACE` |

## Must-have tests (new)
- [ ] referee imports no router; claims atomic under concurrency; lease recovery; loop/hop/TTL guards; deadlock -> failed
- [ ] release ordering; restart resume; 3 notebooks concurrently (no lock errors, no mixed feed records)
- [ ] feed scan: no prompt text, keys, file paths, raw model prose; Mercury never gets student text
- [ ] kill each model/tool (`AASAAN_CHAOS`): notebook still completes
- [ ] keep all fallback, circuit-breaker, secret-masking, old-path tests while strangler flag exists

## Decisions to confirm with the user (not assumed)
1. Section 8 reverses "no agent names in UI": friendly names in a hideable feed. Confirm.
2. Indra weights licence (modified OpenRAIL-M) and hackathon open-source-track eligibility: organisers' question.
3. Dev machine is Windows: run Indra in Docker. Confirmed: Docker 29.4.1 is available locally.
4. Vision tier needs Sarvam Vision key/model name: verify real API before coding, no guessing.
5. Mascot is now the pixel teacher: quiz states (`idle/thinking/cheer/oops/proud`) built on the canvas component.
6. Frontend stays plain JS (no framework); LangGraph pilot only after M4 against the baseline.
7. Earlier agreed strangler flag `AASAAN_ORCHESTRATOR` (per notebook + global off) stays.

## Risks
- Scope: ~13 PRs; keep each reviewable. Foundation first, nothing else starts before its tests pass.
- SQLite contention with agents pulling work: signals not polling, short transactions.
- Thin-content fix depends on tuning depth minimums against baseline.
- OCR in a subprocess (multi-process guard), page cap and timeouts.
