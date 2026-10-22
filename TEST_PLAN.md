# Aasaan Test Plan: 15 Riskiest Failure Points

This document analyzes the architecture, execution engine, worker, model router, web tools, and resilience subsystems of **Aasaan** to identify the 15 riskiest failure points. Each entry details the architectural risk, failure mechanism, potential impact, and how to verify it under test doubles and live conditions.

---

## Architecture Overview & Subsystem Boundaries

```
[Student Request] ──> engine.create_notebook ──> SQLite (jobs: notebook)
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
           worker.py (Lane 1)   worker.py (Lane 2)
           notebook jobs        chapter jobs (concurrency: 2)
                    │                   │
                    ▼                   ▼
           engine.phase0        engine.build_chapter
           (Intake/Architect)   (Curator/Teacher/Critic/Quizzer)
                    │                   │
                    └─────────┬─────────┘
                              ▼
                        router.py ──> resilience.py (CircuitBreaker / Semaphores)
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
              Model Providers     web.py / tools.py
            (Gemini/Sarvam/Jev)  (SSRF Guard / ToolRunner)
```

---

## 15 Riskiest Failure Points

### 1. Notebook Status Stuck in `generating` Permanently When a Chapter Fails
* **Subsystem**: `backend/app/engine.py` (`_refresh_status`, `process_job`), `backend/app/worker.py`
* **Risk & Failure Mechanism**:
  When Chapter 1 finishes drafting, the notebook status changes to `generating`. Remaining chapters are queued into the `jobs` table. If Chapter 2 (or any subsequent background chapter) fails all 3 retry attempts, `process_job` sets that chapter's status to `failed`. 
  `engine._refresh_status()` calculates notebook status as:
  ```python
  if all(c["status"] == "ready" for c in chs):
      status = "complete"
  elif chs[0]["status"] == "ready":
      status = "generating"
  ```
  Because Chapter 2 is `failed`, `all(...)` is `False`. Since Chapter 1 is `ready`, `status` is evaluated as `"generating"`. When all pending jobs in the queue complete, the notebook status remains `"generating"` in the database forever. The frontend UI continues showing the "Preparing your notebook..." spinner indefinitely, never informing the student that generation concluded with failures.
* **How to Test**:
  1. Initialize a notebook with 3 chapters using `FakeProvider`.
  2. Cause Chapter 2's generation to fail 3 consecutive times (e.g. by injecting `AllFailed` into `build_chapter`).
  3. Execute `worker.run_until_idle()`.
  4. Query `store.get_notebook(nid)` and inspect `status`. Verify whether it is stuck in `"generating"` or correctly transitions to a completed/partial-failure state.

---

### 2. Proposal `remove_chapter` Deletes Chapter But Leaves Orphaned Queued Jobs
* **Subsystem**: `backend/app/edits.py` (`apply`), `backend/app/engine.py` (`process_job`)
* **Risk & Failure Mechanism**:
  When a student accepts an edit proposal to remove a chapter (`remove_chapter`), `edits.apply()` calls `store.delete_chapter(cid)`. However, unlike `undo()` (which properly calls `store.cancel_chapter_jobs(cid)`), `apply()` does **not** cancel or delete pending/queued jobs in the `jobs` table for that chapter.
  When the background worker picks up the job, `engine.build_chapter(cid)` queries `self._chapter(cid)`, which raises `UserError("unknown chapter")`. The job fails, gets rescheduled with backoff, and retries 3 times before being marked failed, needlessly consuming worker slots and logging error traces.
* **How to Test**:
  1. Create a notebook with chapters 1, 2, 3 where chapters 2 and 3 have queued jobs.
  2. Apply an edit proposal that removes chapter 2 (`remove_chapter`).
  3. Inspect `store.pending_jobs()` for jobs matching `chapter_id == cid_2`.
  4. Run `engine.process_job()` on that job and verify if it attempts to execute a non-existent chapter.

---

### 3. Server-Side Request Forgery (SSRF) Bypass via HTTP 30x Redirects
* **Subsystem**: `backend/app/web.py` (`is_safe_url`, `http_get`, `fetch_page`, `verify_link`)
* **Risk & Failure Mechanism**:
  `web.is_safe_url(url)` checks that the initial URL does not resolve to private, loopback, or cloud-metadata IP ranges. However, `http_get()` invokes `httpx.get(..., follow_redirects=True)`. If a public web server returns an HTTP 301/302/307 redirect pointing to an internal address (e.g. `http://127.0.0.1:8000/api/admin/events` or `http://169.254.169.254/latest/meta-data/`), `httpx` follows the redirect without re-validating the target URL against `is_safe_url`. This allows malicious links to pivot through `AasaanBot` to private services.
* **How to Test**:
  1. Mock `http_get` with a route where `http://public-domain.com/link` returns HTTP 302 to `http://127.0.0.1:8000/api/admin/events`.
  2. Call `web.fetch_page()` or `web.verify_link()`.
  3. Assert that the request to the loopback address is blocked and not followed.

---

### 4. Prioritizing Retrying Chapters Does Not Reset Exponential Backoff Delay
* **Subsystem**: `backend/app/engine.py` (`prioritize`), `backend/app/db.py` (`pending_jobs`, `set_job`)
* **Risk & Failure Mechanism**:
  When a chapter job fails on attempt 1 or 2, `process_job` reschedules it with an exponential backoff delay: `delay_s = retry_delay_s * attempts`, writing `scheduled_at = now + delay_s` to the `jobs` table.
  When a student clicks that chapter to read it, the frontend calls `POST /api/chapters/{cid}/prioritize`, triggering `engine.prioritize(cid)`. `engine.prioritize()` updates `chapters.priority = 50`, but does **not** update or reset `jobs.scheduled_at`.
  Because `pending_jobs()` filters by `WHERE j.state='queued' AND j.scheduled_at <= ?`, the chapter job remains blocked in the future and will not run until the delay expires, defeating the queue-jumping mechanism.
* **How to Test**:
  1. Trigger a transient failure on a chapter job so it is rescheduled with `scheduled_at = now + 15.0`.
  2. Call `engine.prioritize(cid)`.
  3. Query `store.pending_jobs()`. Check if the prioritized job is immediately returned as due.

---

### 5. Double-Charging Notebook Budgets on Retryable Tool Failures
* **Subsystem**: `backend/app/tools.py` (`ToolRunner.run`, `Budgets`)
* **Risk & Failure Mechanism**:
  In `ToolRunner.run()`, `self.budgets.charge(sp)` is called inside the retry loop (`for attempt in range(sp.retries + 1)`). If a call to Firecrawl, Jina, or YouTube encounters a retryable failure (such as an HTTP 429 or 503) and succeeds on attempt 1, the notebook budget is charged twice for a single logical tool request. This leads to premature exhaustion of the notebook's tool budget (`FIRECRAWL_MAX_CALLS_PER_NOTEBOOK`, `JINA_MAX_CALLS_PER_NOTEBOOK`, etc.).
* **How to Test**:
  1. Configure a tool spec with a budget of 2 and `retries = 1`.
  2. Set `current_notebook` context to `"nb_test"`.
  3. Execute a tool call where attempt 0 raises a retryable 500 error and attempt 1 succeeds.
  4. Check `runner.budgets.usage("nb_test", tool_name)`. Verify whether it is charged 1 or 2 times.

---

### 6. Corrupted or Outdated Cache Entries Poisoning Model Router Retries
* **Subsystem**: `backend/app/router.py` (`call`, `_cache_key`), `backend/app/db.py` (`cache_get`, `cache_set`)
* **Risk & Failure Mechanism**:
  `router.call()` checks SQLite cache via `_cache_key(model_id, prompt, images)`. If a corrupted or malformed response is stored in `cache`, `router.call()` loads it and passes it to `validator()`. When validation fails, it attempts a repair prompt on attempt 1, but never invalidates or evicts the bad cache row from SQLite. Subsequent calls with identical input prompts continue to load the corrupted cache on attempt 0.
* **How to Test**:
  1. Inject an invalid JSON string into `cache` under the key corresponding to a model and prompt.
  2. Call `router.call()`.
  3. Assert whether the invalid cache entry is cleared or if subsequent calls continue hitting the poisoned cache.

---

### 7. Unbounded Memory Leak in Web Scraper State (`_domain_sems` & `_robots`)
* **Subsystem**: `backend/app/web.py` (`_domain_sems`, `_robots`, `_domain_sem`, `allowed_by_robots`)
* **Risk & Failure Mechanism**:
  `web.py` maintains global dictionaries:
  - `_domain_sems: dict[str, threading.Semaphore]`
  - `_robots: dict[str, tuple[float, RobotFileParser]]`
  Neither dictionary enforces a maximum entry cap or LRU eviction policy. When scraping or curating links across thousands of distinct websites, memory grows monotonically, leading to potential memory pressure in long-running container deployments.
* **How to Test**:
  1. Call `web.allowed_by_robots()` and `web.fetch_page()` across 1,000 synthetic domain origins.
  2. Inspect `len(web._domain_sems)` and `len(web._robots)`. Verify whether any cap or eviction occurs.

---

### 8. Unsynchronized Concurrent Mutation of Router Circuit Breakers
* **Subsystem**: `backend/app/router.py` (`_breaker`), `backend/app/resilience.py` (`CircuitBreaker`)
* **Risk & Failure Mechanism**:
  `ToolRunner._breakers` protects dictionary access with `with self._lock:`. However, `ModelRouter._breakers: dict[str, CircuitBreaker]` has no lock guarding `_breakers.get()` and `_breakers[model] = ...`. In a multi-threaded server with multiple concurrent chapter jobs, race conditions can cause instances to be recreated or overwritten, losing tracked consecutive failure counts and tripping states.
* **How to Test**:
  1. Spawn multiple threads concurrently invoking `router._breaker("gemma_31b")` with simulated failures.
  2. Verify that breaker instances and failure counts remain consistent across threads without state loss.

---

### 9. Discarded Quizzer Futures Leaking Execution in Critic Revision Loops
* **Subsystem**: `backend/app/engine.py` (`_build_chapter`)
* **Risk & Failure Mechanism**:
  In `_build_chapter()`, `quiz_future = pool.submit(quizzer.run, ...)` runs concurrently with `critic.run()`. When the Critic rejects a draft, the revision loop starts another attempt. The new iteration overwrites `quiz_future = pool.submit(...)`. The original future from attempt 0 is neither cancelled nor joined; because the thread pool has `max_workers = 2`, the abandoned quizzer future continues consuming threads and CPU cycles, delaying subsequent work.
* **How to Test**:
  1. Configure `FakeProvider` so Critic rejects draft 0 and approves draft 1.
  2. Instrument or inspect thread pool futures in `_build_chapter`.
  3. Verify whether abandoned futures from rejected drafts are cancelled or permitted to run to completion.

---

### 10. Background Worker Misses Job Updates Due to Non-Atomic State Transitions
* **Subsystem**: `backend/app/worker.py` (`run`, `_run_one`), `backend/app/engine.py` (`process_job`), `backend/app/db.py`
* **Risk & Failure Mechanism**:
  In `process_job`, the worker does:
  ```python
  job = self.store.get_job(jid)
  ...
  self.store.set_job(jid, "running", attempts=attempts)
  ```
  The check and state transition are separate non-atomic SQL queries without `WHERE state = 'queued'`. If two workers (or `run_until_idle` and the async worker loop) fetch the job simultaneously, both can pick up and execute the same job concurrently, resulting in duplicated LLM calls and conflicting database writes.
* **How to Test**:
  1. Enqueue a chapter job.
  2. Invoke `process_job(jid)` concurrently from two worker threads.
  3. Verify whether both threads execute `build_chapter` simultaneously or if atomic check-and-set prevents duplicate execution.

---

### 11. Multi-Op Proposal Applies Consecutive Additions in Inverted Order
* **Subsystem**: `backend/app/edits.py` (`apply`), `backend/app/agents/editor.py` (`validate_ops`)
* **Risk & Failure Mechanism**:
  If a proposal specifies two `add_chapter` operations inserting after the same position (e.g. op 1 adds Chapter A after Chapter 2; op 2 adds Chapter B after Chapter 2), `validate_ops` accepts both. During `apply()`, op 1 inserts Chapter A at position 3. Op 2 then re-calculates `order.index(after) + 1` and inserts Chapter B at position 3, shifting Chapter A down to position 4. The applied chapter order becomes [B, A] rather than the requested [A, B].
* **How to Test**:
  1. Construct a proposal with two sequential `add_chapter` operations referencing the same `after_id`.
  2. Call `edits.apply()`.
  3. Verify the final positions and titles of the newly inserted chapters.

---

### 12. Premature Circuit Breaker Lockout During Ephemeral 429 Bursts
* **Subsystem**: `backend/app/resilience.py` (`CircuitBreaker`), `backend/app/router.py`
* **Risk & Failure Mechanism**:
  `CircuitBreaker` has a default threshold of 2 consecutive failures (`circuit_threshold: 2`) and a cooldown of 90 seconds (`circuit_cooldown_s: 90`). On Google AI Studio free tier, a momentary concurrency spike (e.g. two parallel chapters firing Teacher and Critic simultaneously) can return two rapid 429s within 1 second. The router immediately trips the circuit for 90 seconds, completely disabling the primary model for all subsequent operations even if the rate limit cleared after 2 seconds.
* **How to Test**:
  1. Trigger 2 consecutive 429 responses on `gemma_31b`.
  2. Check `circuit_open("gemma_31b")`.
  3. Test whether the circuit breaker respects `Retry-After` headers or unconditionally imposes the 90s penalty.

---

### 13. Pre-Fetch Etiquette Violation in Link Verification (`verify_link`)
* **Subsystem**: `backend/app/web.py` (`verify_link`)
* **Risk & Failure Mechanism**:
  In `verify_link(c: Candidate)`, `http_get(c.url)` is executed at line 499 before checking `allowed_by_robots(final)` at line 508. Even though candidates violating `robots.txt` are marked with `c.extra["drop"] = "robots.txt"`, the full HTTP GET request and page download has already occurred. This breaches standard web etiquette and exposes the crawler to sensitive disallowed endpoints.
* **How to Test**:
  1. Set up a route with `/robots.txt` disallowing `/private-spec`.
  2. Call `web.verify_link(Candidate(id="c1", kind="link", url="http://test.com/private-spec", title="Test", source="test.com"))`.
  3. Assert whether `http_get("http://test.com/private-spec")` was dispatched prior to checking robots exclusion.

---

### 14. Unhandled Crash on Scanned/Corrupt PDFs During Page Recovery
* **Subsystem**: `backend/app/engine.py` (`_read_source`, `_recover_pages`), `backend/app/agents/intake.py`
* **Risk & Failure Mechanism**:
  When processing an uploaded PDF without a text layer, `_recover_pages` calls `intake.render_page_png()`. If the PDF contains corrupt streams or malformed vector objects, `pypdfium2` can raise unhandled memory errors or rendering exceptions. If not cleanly caught inside `_recover_pages`, the exception causes `_read_source` to raise, failing the notebook job rather than gracefully classifying the file as unreadable in Resources.
* **How to Test**:
  1. Create a PDF source containing truncated or corrupt PDF objects.
  2. Run `_read_source()`.
  3. Verify if an issue is recorded in `source_meta` or if an unhandled exception bubbles up and crashes the job.

---

### 15. Race Condition and Orphaned Threads in Illustrator Diagram Generation
* **Subsystem**: `backend/app/engine.py` (`_build_chapter`), `backend/app/agents/illustrator.py`
* **Risk & Failure Mechanism**:
  In `_build_chapter()`, `illo_future = illo_pool.submit(illustrator.run, ...)`. The future is awaited with `illo_future.result(timeout=AASAAN_ILLUSTRATOR_TIMEOUT_S)`. On `FutureTimeout`, `illo_pool.shutdown(wait=False, cancel_futures=True)` is called. Since Python's `ThreadPoolExecutor` cannot terminate executing threads, the thread continues running in the background until the LLM call finishes. Under high load or frequent timeouts, zombie threads accumulate.
* **How to Test**:
  1. Simulate an illustrator call that hangs or takes longer than `AASAAN_ILLUSTRATOR_TIMEOUT_S`.
  2. Verify that timeout handling proceeds without crashing `_build_chapter`.
  3. Measure active thread count before and after shutdown.

---

## Top 5 Failure Points Selected for Pytest Suite

The following 5 failure points have been prioritized for automated verification in `backend/tests/test_riskiest_failures.py`:

| Test Name | Target Failure Point | Severity |
| :--- | :--- | :--- |
| `test_notebook_stuck_in_generating_when_chapter_fails` | FP-1: Notebook status stuck in `generating` | High |
| `test_remove_chapter_leaves_orphaned_jobs` | FP-2: Proposal `remove_chapter` leaves orphaned jobs | High |
| `test_ssrf_redirect_bypass` | FP-3: SSRF guard bypassed by HTTP 30x redirect | Critical |
| `test_prioritize_does_not_clear_retry_delay` | FP-4: Prioritizing chapter ignores retry delay | Medium |
| `test_tool_runner_double_charges_budget_on_retry` | FP-5: ToolRunner charges budget twice on retry | Medium |
