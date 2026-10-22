"""Aasaan Concurrency & Stress Testing Suite.
Simulates multiple concurrent learners, background chapter generation,
parallel database access, and model call rate limiting against http://localhost:8000.

Usage:
    python scripts/stress_test.py [--url http://localhost:8000] [--concurrency 3] [--notebooks 3]
"""
from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

GOALS = [
    "I want to learn Data Structures and Algorithms in Python",
    "Teach me SQL and Database Management Systems from scratch",
    "Master Computer Networking and TCP/IP fundamentals",
    "Learn Operating Systems: processes, threads, and memory management",
    "Understanding Machine Learning basics and linear algebra",
]


@dataclass
class StressMetrics:
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    rate_limited_429: int = 0
    db_locked_errors: int = 0
    server_errors_500: int = 0
    latencies: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class ConcurrencyTester:
    def __init__(self, base_url: str = "http://localhost:8000", timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.metrics = StressMetrics()

    async def create_learner(self, client: httpx.AsyncClient) -> str:
        r = await client.post(f"{self.base_url}/api/learners", json={})
        r.raise_for_status()
        return r.json()["learner_id"]

    async def create_session(self, client: httpx.AsyncClient, learner_id: str) -> str:
        r = await client.post(f"{self.base_url}/api/session/open", json={"learner_id": learner_id})
        r.raise_for_status()
        return r.json()["session_id"]

    async def create_notebook(self, client: httpx.AsyncClient, learner_id: str, goal: str) -> str:
        t0 = time.perf_counter()
        r = await client.post(
            f"{self.base_url}/api/notebooks",
            data={"learner_id": learner_id, "text": goal, "language": "en"},
        )
        latency = time.perf_counter() - t0
        self.record_response(r, latency)
        r.raise_for_status()
        return r.json()["notebook_id"]

    async def poll_notebook_progress(
        self, client: httpx.AsyncClient, nid: str, max_duration: float = 120.0, poll_interval: float = 1.0
    ) -> dict[str, Any]:
        """Polls notebook until chapter 1 is ready or timeout occurs."""
        end_time = time.time() + max_duration
        while time.time() < end_time:
            t0 = time.perf_counter()
            try:
                r = await client.get(f"{self.base_url}/api/notebooks/{nid}")
                latency = time.perf_counter() - t0
                self.record_response(r, latency)
                if r.status_code == 200:
                    data = r.json()
                    status = data.get("status")
                    chapters = data.get("chapters", [])
                    ready_count = sum(1 for c in chapters if c.get("status") == "ready")
                    if ready_count >= 1 or status in ("complete", "failed"):
                        return data
            except Exception as e:
                self.metrics.errors.append(f"Poll error for {nid}: {e}")
            await asyncio.sleep(poll_interval)
        return {"status": "timeout", "chapters": []}

    async def simulate_active_learner(
        self, client: httpx.AsyncClient, learner_idx: int, goal: str
    ) -> dict[str, Any]:
        """Simulates one complete learner session running concurrently with others."""
        t_start = time.perf_counter()
        print(f"[{learner_idx}] Starting session: '{goal[:40]}...'")
        try:
            learner_id = await self.create_learner(client)
            session_id = await self.create_session(client, learner_id)
            nid = await self.create_notebook(client, learner_id, goal)
            print(f"[{learner_idx}] Notebook created: {nid}. Waiting for outline and Chapter 1...")

            nb_data = await self.poll_notebook_progress(client, nid, max_duration=90.0)
            status = nb_data.get("status", "unknown")
            chapters = nb_data.get("chapters", [])
            ready_chapters = [c for c in chapters if c.get("status") == "ready"]

            # If a chapter is ready, simulate student reading notes and sending a chat doubt
            if ready_chapters:
                first_ch = ready_chapters[0]
                t0 = time.perf_counter()
                chat_res = await client.post(
                    f"{self.base_url}/api/notebooks/{nid}/chat",
                    json={
                        "learner_id": learner_id,
                        "session_id": session_id,
                        "question": "Can you explain the main concept of this chapter?",
                        "chapter_id": first_ch["id"],
                    },
                    timeout=30.0,
                )
                self.record_response(chat_res, time.perf_counter() - t0)

            # If remaining chapters are queued, simulate student prioritizing chapter 3
            queued_chapters = [c for c in chapters if c.get("status") == "queued"]
            if queued_chapters:
                target_ch = queued_chapters[-1]
                t0 = time.perf_counter()
                prio_res = await client.post(
                    f"{self.base_url}/api/chapters/{target_ch['id']}/prioritize",
                    timeout=10.0,
                )
                self.record_response(prio_res, time.perf_counter() - t0)

            total_elapsed = time.perf_counter() - t_start
            print(f"[{learner_idx}] Finished session in {total_elapsed:.1f}s (status: {status}, ready: {len(ready_chapters)})")
            return {"learner_idx": learner_idx, "nid": nid, "status": status, "elapsed_s": total_elapsed}

        except Exception as e:
            self.metrics.failed_requests += 1
            err_msg = f"Learner [{learner_idx}] failed: {type(e).__name__}: {e}"
            self.metrics.errors.append(err_msg)
            print(f"[{learner_idx}] ERROR: {err_msg}")
            return {"learner_idx": learner_idx, "error": str(e)}

    def record_response(self, response: httpx.Response, latency: float) -> None:
        self.metrics.total_requests += 1
        self.metrics.latencies.append(latency)
        if response.status_code == 200:
            self.metrics.successful_requests += 1
        elif response.status_code == 429:
            self.metrics.rate_limited_429 += 1
            self.metrics.errors.append("HTTP 429: Too Many Requests / Rate Limit Exceeded")
        elif response.status_code >= 500:
            self.metrics.server_errors_500 += 1
            body_snippet = response.text[:200]
            if "database is locked" in body_snippet.lower():
                self.metrics.db_locked_errors += 1
            self.metrics.errors.append(f"HTTP {response.status_code}: {body_snippet}")
        else:
            self.metrics.failed_requests += 1

    async def run_stress_test(self, num_notebooks: int = 3, concurrency: int = 3) -> None:
        print(f"\n{'='*70}")
        print(f"AASAAN STRESS TEST: {num_notebooks} simultaneous courses (concurrency: {concurrency})")
        print(f"Target URL: {self.base_url}")
        print(f"{'='*70}\n")

        semaphore = asyncio.Semaphore(concurrency)

        async def worker_task(idx: int, goal: str):
            async with semaphore:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    return await self.simulate_active_learner(client, idx, goal)

        tasks = [
            asyncio.create_task(worker_task(i + 1, GOALS[i % len(GOALS)]))
            for i in range(num_notebooks)
        ]

        t0 = time.perf_counter()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        total_time = time.perf_counter() - t0

        self.print_summary(total_time, results)

    def print_summary(self, total_time: float, results: list[Any]) -> None:
        print(f"\n{'='*70}")
        print("CONCURRENCY & STRESS TEST RESULTS")
        print(f"{'='*70}")
        print(f"Total Test Duration:       {total_time:.2f} s")
        print(f"Total HTTP Requests:       {self.metrics.total_requests}")
        print(f"Successful Requests (200): {self.metrics.successful_requests}")
        print(f"Failed Requests:           {self.metrics.failed_requests}")
        print(f"Rate Limits (429):         {self.metrics.rate_limited_429}")
        print(f"Server Errors (5xx):       {self.metrics.server_errors_500}")
        print(f"SQLite Locks Detected:     {self.metrics.db_locked_errors}")

        if self.metrics.latencies:
            avg_lat = sum(self.metrics.latencies) / len(self.metrics.latencies)
            max_lat = max(self.metrics.latencies)
            p95 = sorted(self.metrics.latencies)[int(len(self.metrics.latencies) * 0.95)]
            print(f"Average Request Latency:   {avg_lat * 1000:.1f} ms")
            print(f"95th Percentile Latency:   {p95 * 1000:.1f} ms")
            print(f"Max Request Latency:       {max_lat * 1000:.1f} ms")

        if self.metrics.errors:
            print(f"\nUnique Errors Encountered ({len(self.metrics.errors)} total):")
            for err in set(self.metrics.errors)[:8]:
                print(f"  * {err}")
        print(f"{'='*70}\n")


def main():
    parser = argparse.ArgumentParser(description="Run concurrency and stress test against Aasaan")
    parser.add_argument("--url", default="http://localhost:8000", help="Base URL of running server")
    parser.add_argument("--concurrency", type=int, default=3, help="Max simultaneous learners")
    parser.add_argument("--notebooks", type=int, default=3, help="Total notebooks to generate")
    args = parser.parse_args()

    tester = ConcurrencyTester(base_url=args.url)
    asyncio.run(tester.run_stress_test(num_notebooks=args.notebooks, concurrency=args.concurrency))


if __name__ == "__main__":
    main()
