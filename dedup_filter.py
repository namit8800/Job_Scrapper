"""
dedup_filter.py
Filters a scraped jobs file against a persistent seen-jobs file
so the same job never appears twice across separate GitHub Actions runs.

Also maintains a cumulative all-unique-jobs file — keeps growing with
every unique job ever found, full details, no duplicates.

Usage: python3 dedup_filter.py <source>
  <source> is one of: internshala, naukri, indeed

Reads:  <source>_jobs.json (this run's scrape)
Reads/Writes: seen_jobs_<source>.json (persistent dedup memory, committed back to repo)
Reads/Writes: all_unique_jobs_<source>.json (persistent full job data, committed back to repo)
Writes: <source>_jobs_new.json (only genuinely new jobs from this run, for the artifact)
"""

import json
import os
import sys

SOURCE = sys.argv[1] if len(sys.argv) > 1 else "internshala"

SCRAPED_FILE = f"{SOURCE}_jobs.json"
SEEN_FILE    = f"seen_jobs_{SOURCE}.json"
ALL_FILE     = f"all_unique_jobs_{SOURCE}.json"
NEW_FILE     = f"{SOURCE}_jobs_new.json"


def job_key(job: dict) -> str:
    """Unique key per job — apply_link is the most stable identifier."""
    return job.get("apply_link") or f"{job.get('title')}__{job.get('company')}__{job.get('posting_date')}"


def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set) -> None:
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)


def load_all_jobs() -> list:
    if os.path.exists(ALL_FILE):
        with open(ALL_FILE, "r") as f:
            return json.load(f)
    return []


def save_all_jobs(jobs: list) -> None:
    with open(ALL_FILE, "w") as f:
        json.dump(jobs, f, indent=2)


def main():
    if not os.path.exists(SCRAPED_FILE):
        print(f"No scraped file found at {SCRAPED_FILE} — nothing to dedup.")
        with open(NEW_FILE, "w") as f:
            json.dump([], f)
        return

    with open(SCRAPED_FILE, "r") as f:
        scraped_jobs = json.load(f)

    seen     = load_seen()
    all_jobs = load_all_jobs()
    new_jobs = []

    for job in scraped_jobs:
        key = job_key(job)
        if key not in seen:
            new_jobs.append(job)
            all_jobs.append(job)
            seen.add(key)

    save_seen(seen)
    save_all_jobs(all_jobs)

    with open(NEW_FILE, "w") as f:
        json.dump(new_jobs, f, indent=2)

    print(f"[{SOURCE}] Scraped this run: {len(scraped_jobs)}")
    print(f"[{SOURCE}] Already seen (skipped): {len(scraped_jobs) - len(new_jobs)}")
    print(f"[{SOURCE}] Genuinely new: {len(new_jobs)}")
    print(f"[{SOURCE}] Total unique jobs all-time: {len(all_jobs)}")


if __name__ == "__main__":
    main()
