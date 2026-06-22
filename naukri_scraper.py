"""
naukri_scraper.py
SyncUp — Project 02 / Job Scraper
Naukri scraper with stealth mode to avoid bot detection.
"""

import json
import logging
import os
import re
import requests
from datetime import datetime
from typing import Optional, Tuple
from playwright.sync_api import sync_playwright
try:
    from playwright_stealth import stealth_sync
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False

# ── Constants ─────────────────────────────────────────────────────────────────
SOURCE_PLATFORM   = "Naukri"
OUTPUT_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "naukri_jobs.json")
SERVER_URL        = os.environ.get("SYNCUP_SERVER_URL", "http://localhost:3000/api/jobs")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
HEADLESS          = os.environ.get("HEADLESS", "false").lower() == "true"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    filename=os.path.join(os.path.dirname(os.path.abspath(__file__)), "scraper.log"),
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# ── Slack alert ───────────────────────────────────────────────────────────────
def alert_slack(message: str) -> None:
    if not SLACK_WEBHOOK_URL:
        return
    try:
        requests.post(SLACK_WEBHOOK_URL, json={"text": message}, timeout=5)
    except Exception:
        pass


# ── URL builder ───────────────────────────────────────────────────────────────
def build_url(role_slug: str, location_slug: str, is_remote: bool) -> str:
    # Special "any" mode — Naukri's general latest-jobs page, no role/location filter
    if role_slug in ("any", "all", "") and location_slug in ("any", "all", ""):
        return "https://www.naukri.com/jobs-in-india?jobAge=1"
    if is_remote:
        # Use India-wide search with work-from-home keyword — avoids Cloudflare block
        return f"https://www.naukri.com/{role_slug}-jobs?jobAge=7&wfhType=2"
    return f"https://www.naukri.com/{role_slug}-jobs-in-{location_slug}"


# ── DOM helpers ───────────────────────────────────────────────────────────────
def get_text(element, selector: str) -> str:
    el = element.query_selector(selector)
    return el.inner_text().strip() if el else "N/A"


def parse_skills(job_element) -> list:
    return [
        el.inner_text().strip()
        for el in job_element.query_selector_all("ul.tags-gt li.tag-li")
        if el.inner_text().strip()
    ]


def parse_posting_date(job_element) -> str:
    el = job_element.query_selector("span.job-post-day")
    return el.inner_text().strip() if el else "N/A"


def parse_job_type(job_element) -> str:
    el = job_element.query_selector("span.expwdth")
    return el.inner_text().strip() if el else "N/A"


# ── Salary helpers ────────────────────────────────────────────────────────────
def parse_salary_amount(salary_text: str) -> Optional[int]:
    if not salary_text or salary_text.strip() in ("N/A", "Not disclosed", "Not provided", ""):
        return None
    lacs_match = re.search(r"([\d.]+)\s*[Ll]ac", salary_text.replace(",", ""))
    if lacs_match:
        try:
            return int(float(lacs_match.group(1)) * 100_000)
        except ValueError:
            return None
    numbers = re.findall(r"\d+", salary_text.replace(",", ""))
    try:
        return int(numbers[0]) if numbers else None
    except ValueError:
        return None


def passes_salary_filter(salary_text: str, min_salary: Optional[int], max_salary: Optional[int]) -> bool:
    if min_salary is None and max_salary is None:
        return True
    amount = parse_salary_amount(salary_text)
    if amount is None:
        return False
    if min_salary is not None and amount < min_salary:
        return False
    if max_salary is not None and amount > max_salary:
        return False
    return True


# ── Deduplication ─────────────────────────────────────────────────────────────
def is_duplicate(seen: list, new_job: dict) -> bool:
    return any(
        j.get("title")        == new_job.get("title")
        and j.get("company")  == new_job.get("company")
        and j.get("posting_date") == new_job.get("posting_date")
        for j in seen
    )


# ── Core scraper ──────────────────────────────────────────────────────────────
def scrape_naukri(
    role: str,
    location: str,
    min_salary: Optional[int] = None,
    max_salary: Optional[int] = None,
) -> list:
    role_slug     = role.strip().lower().replace(" ", "-")
    location_slug = location.strip().lower().replace(" ", "-")
    is_remote     = location_slug in ("remote", "work-from-home", "wfh")
    url           = build_url(role_slug, location_slug, is_remote)

    print(f"\n[SEARCH] Opening: {url}")
    scraped = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1280,900",
            ]
        )

        # ── Stealth context — mimics a real browser ───────────────────────
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            extra_http_headers={
                "Accept-Language": "en-IN,en;q=0.9",
                "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection":      "keep-alive",
            }
        )

        # ── Hide webdriver flag that Naukri checks ────────────────────────
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-IN', 'en'] });
            window.chrome = { runtime: {} };
        """)

        page = context.new_page()

        # Apply stealth patches — hides 20+ Cloudflare fingerprint signals
        if STEALTH_AVAILABLE:
            stealth_sync(page)
            print("[STEALTH] Stealth mode active")
        else:
            print("[WARN]  playwright-stealth not installed — run: pip install playwright-stealth")

        try:
            page.goto(url, timeout=30_000, wait_until="networkidle")
            page.wait_for_timeout(12_000)  # extra wait for JS to fully render
        except Exception as e:
            msg = f"[Naukri] Failed to load {url}: {e}"
            logging.error(msg)
            print(f"[ERROR] Could not load page: {e}")
            alert_slack(f"[ERROR] Naukri scraper failed\n{msg}")
            browser.close()
            return []

        listings = page.query_selector_all("div.srp-jobtuple-wrapper")
        print(f"[FOUND] Found {len(listings)} listings on page\n")

        if not listings:
            msg = f"[Naukri] No listings found at {url} — may be blocked or layout changed"
            logging.error(msg)
            alert_slack(f"[WARN] Naukri 0 listings\n{url}")

        for job in listings:
            try:
                title_el   = job.query_selector("a.title")
                title      = title_el.inner_text().strip() if title_el else "N/A"
                apply_link = title_el.get_attribute("href") if title_el else "N/A"

                company_el = job.query_selector("a.comp-name")
                company    = company_el.inner_text().strip() if company_el else "N/A"

                loc_el        = job.query_selector("span.locWdth, span[class*='locWdth']")
                location_text = loc_el.inner_text().strip().lower() if loc_el else "N/A"

                sal_el = (
                    job.query_selector("span.sal") or
                    job.query_selector("span.sal-wrap span") or
                    job.query_selector(".sal-wrap span") or
                    job.query_selector("span[class*='sal']")
                )
                salary_text = sal_el.inner_text().strip() if sal_el else "Not disclosed"

                if not passes_salary_filter(salary_text, min_salary, max_salary):
                    continue

                job_record = {
                    "title":           title,
                    "company":         company,
                    "location":        location_text,
                    "job_type":        parse_job_type(job),
                    "salary":          salary_text,
                    "salary_numeric":  parse_salary_amount(salary_text),
                    "skills_required": parse_skills(job),
                    "posting_date":    parse_posting_date(job),
                    "apply_link":      apply_link,
                    "source_platform": SOURCE_PLATFORM,
                    "scraped_at":      datetime.utcnow().isoformat() + "Z",
                }

                if is_duplicate(scraped, job_record):
                    print(f"  [SKIP]  Duplicate: {job_record['title']} @ {job_record['company']}")
                    continue

                scraped.append(job_record)
                sal = f"Rs {job_record['salary_numeric']:,}" if job_record["salary_numeric"] else job_record["salary"]
                print(f"  [OK] {job_record['title']} — {job_record['company']} | {sal}")

            except Exception as e:
                logging.error(f"[Naukri] Card error: {e}")
                print(f"  [WARN]  Card error: {e}")
                continue

        browser.close()

    return scraped


# ── Save to disk ──────────────────────────────────────────────────────────────
def save_jobs(jobs: list, filepath: str) -> None:
    with open(filepath, "w") as f:
        json.dump(jobs, f, indent=2)
    print(f"\n[SAVED] Saved {len(jobs)} jobs -> {filepath}")


# ── Push to server ────────────────────────────────────────────────────────────
def push_to_server(jobs: list, api_url: str = SERVER_URL) -> None:
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
            else:
                failed += 1
                logging.error(f"Server rejected '{job['title']}': {res.status_code}")
        except requests.exceptions.ConnectionError:
            print("\n[ERROR] Cannot connect to server at http://localhost:3000")
            print("   Start it first:  node api/server.js")
            break
        except Exception as e:
            failed += 1
            logging.error(f"Push error '{job['title']}': {e}")
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
        parse_int("  Min salary Rs/year (blank = no minimum): "),
        parse_int("  Max salary Rs/year (blank = no maximum): "),
    )


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    role         = input("Enter role (e.g. python developer, data analyst): ").strip()
    location     = input("Enter location (e.g. bangalore, remote):          ").strip()
    min_s, max_s = prompt_salary_filter()

    jobs = scrape_naukri(role, location, min_salary=min_s, max_salary=max_s)

    if jobs:
        save_jobs(jobs, OUTPUT_FILE)
        push_to_server(jobs)
    else:
        print("\n[WARN]  No jobs found matching your filters.")
        alert_slack(f"[WARN] Naukri 0 results for '{role}' in '{location}'")
