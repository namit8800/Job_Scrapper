"""
internshala_scraper.py
SyncUp — Project 02 / Job Scraper
Internshala scraper — exactly per the project brief.

Brief requirements covered:
  - Fields: title, company, location, job_type, salary, skills_required,
            posting_date, apply_link, source_platform
  - Playwright for JS-rendered pages
  - Modular (one scraper per site, this file is solely Internshala)
  - Deduplication before pushing (title + company + date)
  - Graceful failure + logging when site is down or layout changes
  - Push to Node/Express API
  - headless=True when run via GitHub Actions (HEADLESS env var)
  - Slack alert on failure (SLACK_WEBHOOK_URL env var)
"""

import json
import logging
import os
import re
import requests
from datetime import datetime
from typing import Optional, Tuple
from playwright.sync_api import sync_playwright

# ── Constants ─────────────────────────────────────────────────────────────────
SOURCE_PLATFORM   = "Internshala"
OUTPUT_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "internshala_jobs.json")
SERVER_URL        = os.environ.get("SYNCUP_SERVER_URL", "http://localhost:3000/api/jobs")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
# headless=False locally (watch the browser); GitHub Actions sets HEADLESS=true
HEADLESS          = os.environ.get("HEADLESS", "false").lower() == "true"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    filename=os.path.join(os.path.dirname(os.path.abspath(__file__)), "scraper.log"),
    level=logging.ERROR,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# ── Slack alert (brief: notify team when scraper fails) ──────────────────────

def alert_slack(message: str) -> None:
    """Send a failure alert to Slack. Only runs if SLACK_WEBHOOK_URL is set."""
    if not SLACK_WEBHOOK_URL:
        return
    try:
        requests.post(SLACK_WEBHOOK_URL, json={"text": message}, timeout=5)
    except Exception:
        pass   # don't let alert failure crash anything


# ── URL builder ───────────────────────────────────────────────────────────────

def build_url(role_slug: str, location_slug: str, is_remote: bool) -> str:
    if is_remote:
        return f"https://internshala.com/internships/work-from-home-{role_slug}-internship/"
    return f"https://internshala.com/internships/{role_slug}-internship-in-{location_slug}/"


# ── DOM helpers ───────────────────────────────────────────────────────────────

def get_text(element, selector: str) -> str:
    """Safely extract inner text — returns N/A if element not found."""
    el = element.query_selector(selector)
    return el.inner_text().strip() if el else "N/A"


def parse_skills(job_element) -> list:
    """Extract skill tags from the card."""
    return [
        el.inner_text().strip()
        for el in job_element.query_selector_all(".round_tabs")
        if el.inner_text().strip()
    ]


def parse_posting_date(job_element) -> str:
    """Extract 'X days ago' / 'Today' / 'Just posted' posting date."""
    el = job_element.query_selector(".status-inactive, .actively_hiring, span.posted_by_time")
    if el:
        return el.inner_text().strip()
    for span in job_element.query_selector_all("span"):
        text = span.inner_text().strip().lower()
        if "day" in text or "just posted" in text or "today" in text:
            return span.inner_text().strip()
    return "N/A"


def parse_job_type(job_element) -> str:
    """Extract Part time / Full time / Remote label pills."""
    labels = [
        el.inner_text().strip()
        for el in job_element.query_selector_all(".label_pill, .part_time, .full_time")
        if el.inner_text().strip()
    ]
    return ", ".join(labels) if labels else "Internship"


# ── Salary helpers ────────────────────────────────────────────────────────────

def parse_stipend_amount(stipend_text: str) -> Optional[int]:
    """
    Parse lower-bound monthly stipend as integer.
      "Rs 5,000 - 8,000 /month" -> 5000
      "Rs 10,000 /month"        -> 10000
      "Unpaid" / "N/A"          -> None
    """
    if not stipend_text or stipend_text.strip() in ("N/A", "Unpaid", "Not provided", ""):
        return None
    numbers = re.findall(r"\d+", stipend_text.replace(",", ""))
    try:
        return int(numbers[0]) if numbers else None
    except ValueError:
        return None


def passes_salary_filter(
    stipend_text: str,
    min_salary: Optional[int],
    max_salary: Optional[int],
) -> bool:
    """
    No filter set  -> everything passes.
    Filter set     -> exclude listings with unparseable salary,
                      and exclude those outside the range.
    """
    if min_salary is None and max_salary is None:
        return True                    # no filter — include everything

    amount = parse_stipend_amount(stipend_text)
    if amount is None:
        return False                   # filter active but salary unknown — exclude

    if min_salary is not None and amount < min_salary:
        return False
    if max_salary is not None and amount > max_salary:
        return False
    return True


# ── Deduplication ─────────────────────────────────────────────────────────────

def is_duplicate(seen: list, new_job: dict) -> bool:
    """
    Brief: check if job already exists before pushing.
    Match on title + company + posting_date.
    """
    return any(
        j.get("title")        == new_job.get("title")
        and j.get("company")  == new_job.get("company")
        and j.get("posting_date") == new_job.get("posting_date")
        for j in seen
    )


# ── Core scraper ──────────────────────────────────────────────────────────────

def scrape_internshala(
    role: str,
    location: str,
    min_salary: Optional[int] = None,
    max_salary: Optional[int] = None,
) -> list:
    """
    Scrape Internshala for the given role + location.
    Uses Playwright (handles JS-rendered pages — brief requirement).
    Fails gracefully and logs errors if site is down or layout changes.
    """
    role_slug     = role.strip().lower().replace(" ", "-")
    location_slug = location.strip().lower().replace(" ", "-")
    is_remote     = location_slug in ("remote", "work-from-home", "wfh")
    url           = build_url(role_slug, location_slug, is_remote)

    print(f"\n[SEARCH] Opening: {url}")
    if min_salary is not None or max_salary is not None:
        lo = f"Rs {min_salary:,}" if min_salary is not None else "any"
        hi = f"Rs {max_salary:,}" if max_salary is not None else "any"
        print(f"[SALARY] Salary filter: {lo} – {hi} /month")

    scraped = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page    = browser.new_page()
        page.set_extra_http_headers({"User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )})

        # ── Graceful failure if site is down ──────────────────────────────
        try:
            page.goto(url, timeout=30_000)
            page.wait_for_timeout(5000)
        except Exception as e:
            msg = f"[Internshala] Failed to load {url}: {e}"
            logging.error(msg)
            print(f"[ERROR] Could not load page: {e}")
            alert_slack(f"[ERROR] Internshala scraper failed to load page\n{msg}")
            browser.close()
            return []

        listings = page.query_selector_all(".individual_internship")
        print(f"[FOUND] Found {len(listings)} listings on page\n")

        if not listings:
            msg = f"[Internshala] No listings found at {url} — page layout may have changed"
            logging.error(msg)
            alert_slack(f"[WARN] Internshala scraper found 0 listings — DOM may have changed\n{url}")

        for job in listings:
            # ── Graceful failure per card ──────────────────────────────────
            try:
                location_text = get_text(job, ".locations").lower()
                full_text     = job.inner_text().lower()

                # Location filter — fuzzy match so minor typos still work
                if is_remote:
                    if not any(w in full_text for w in ["remote", "work from home", "wfh"]):
                        continue
                else:
                    loc_clean = location_text.replace(" ", "").replace("-", "")
                    inp_clean = location_slug.replace(" ", "").replace("-", "")
                    if inp_clean not in loc_clean and loc_clean not in inp_clean:
                        continue

                # Apply link
                link_el    = job.query_selector("a.view_detail_button, a[href*='/internship/']")
                href       = link_el.get_attribute("href") if link_el else ""
                apply_link = ("https://internshala.com" + href) if href else "N/A"

                stipend_text = get_text(job, ".stipend")

                # Salary filter
                if not passes_salary_filter(stipend_text, min_salary, max_salary):
                    amount = parse_stipend_amount(stipend_text)
                    label  = f"Rs {amount:,}/mo" if amount else stipend_text
                    print(f"  [FILTERED] Filtered ({label}): {get_text(job, '.job-internship-name')}")
                    continue

                # Build record with all brief-required fields
                job_record = {
                    "title":            get_text(job, ".job-internship-name"),
                    "company":          get_text(job, ".company-name"),
                    "location":         location_text,
                    "job_type":         parse_job_type(job),
                    "salary":           stipend_text,
                    "salary_numeric":   parse_stipend_amount(stipend_text),
                    "skills_required":  parse_skills(job),
                    "posting_date":     parse_posting_date(job),
                    "apply_link":       apply_link,
                    "source_platform":  SOURCE_PLATFORM,
                    "duration":         get_text(job, ".item_body.duration"),
                    "scraped_at":       datetime.utcnow().isoformat() + "Z",
                }

                # Deduplication (brief requirement)
                if is_duplicate(scraped, job_record):
                    print(f"  [SKIP]  Duplicate: {job_record['title']} @ {job_record['company']}")
                    continue

                scraped.append(job_record)
                sal = f"Rs {job_record['salary_numeric']:,}" if job_record["salary_numeric"] else job_record["salary"]
                print(f"  [OK] {job_record['title']} — {job_record['company']} | {sal}/mo")

            except Exception as e:
                # Brief: fail gracefully and log the error
                logging.error(f"[Internshala] Card parse error: {e}")
                print(f"  [WARN]  Skipped one card (logged)")
                continue

        browser.close()

    return scraped


# ── Save to disk ──────────────────────────────────────────────────────────────

def save_jobs(jobs: list, filepath: str) -> None:
    """Overwrite the output file with fresh results from this run."""
    with open(filepath, "w") as f:
        json.dump(jobs, f, indent=2)
    print(f"\n[SAVED] Saved {len(jobs)} jobs -> {filepath}")


# ── Push to Node/Express server (brief requirement) ───────────────────────────

def push_to_server(jobs: list, api_url: str = SERVER_URL) -> None:
    """
    Push each job to the local Node/Express API.
    Brief: deduplication happens here too — server rejects known jobs.
    Start server first: node api/server.js
    """
    pushed = skipped = failed = 0

    for job in jobs:
        try:
            res  = requests.post(api_url, json=job, timeout=10)
            data = res.json()

            if res.status_code == 200 and data.get("status") == "stored":
                pushed += 1
                print(f"  [PUSH] Stored: {job['title']} — {job['company']}")
            elif data.get("status") == "duplicate":
                skipped += 1
                print(f"  [SKIP]  Duplicate on server: {job['title']}")
            else:
                failed += 1
                logging.error(f"Server rejected '{job['title']}': {res.status_code} {res.text}")
                print(f"  [ERROR] Rejected: {job['title']} ({res.status_code})")

        except requests.exceptions.ConnectionError:
            print("\n[ERROR] Cannot connect to server at http://localhost:3000")
            print("   Start it first:  node api/server.js")
            logging.error("Server connection refused")
            alert_slack("[ERROR] Internshala scraper could not connect to local server")
            break
        except Exception as e:
            failed += 1
            logging.error(f"Push error '{job['title']}': {e}")
            print(f"  [ERROR] Error: {job['title']} — {e}")

    print(f"\n[STATS] Server push: {pushed} stored, {skipped} duplicates, {failed} failed")


# ── Salary prompt ─────────────────────────────────────────────────────────────

def prompt_salary_filter() -> Tuple[Optional[int], Optional[int]]:
    use_filter = input("Apply a salary filter? (y/n): ").strip().lower()
    if use_filter != "y":
        return None, None

    def parse_int(prompt):
        val = input(prompt).strip()
        if not val:
            return None
        try:
            return int(val.replace(",", ""))
        except ValueError:
            print("  Invalid — skipping this bound.")
            return None

    return (
        parse_int("  Min stipend Rs/month (blank = no minimum): "),
        parse_int("  Max stipend Rs/month (blank = no maximum): "),
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    role         = input("Enter role (e.g. software, marketing): ").strip()
    location     = input("Enter location (e.g. delhi, remote):   ").strip()
    min_s, max_s = prompt_salary_filter()

    jobs = scrape_internshala(role, location, min_salary=min_s, max_salary=max_s)

    if jobs:
        save_jobs(jobs, OUTPUT_FILE)
        push_to_server(jobs)
    else:
        print("\n[WARN]  No jobs found matching your filters.")
        alert_slack(f"[WARN] Internshala scraper returned 0 results for role='{role}' location='{location}'")
