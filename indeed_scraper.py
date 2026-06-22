"""
indeed_scraper.py
SyncUp — Project 02 / Job Scraper
Indeed.in scraper — uses Playwright with stealth to bypass bot detection.
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
SOURCE_PLATFORM   = "Indeed"
OUTPUT_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "indeed_jobs.json")
SERVER_URL        = os.environ.get("SYNCUP_SERVER_URL", "http://localhost:3000/api/jobs")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
HEADLESS          = os.environ.get("HEADLESS", "false").lower() == "true"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    filename=os.path.join(os.path.dirname(os.path.abspath(__file__)), "scraper.log"),
    level=logging.ERROR,
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


# ── Salary helpers ────────────────────────────────────────────────────────────
def parse_salary_amount(salary_text: str) -> Optional[int]:
    if not salary_text or salary_text.strip() in ("N/A", "Not disclosed", ""):
        return None
    numbers = re.findall(r"\d+", salary_text.replace(",", ""))
    try:
        return int(numbers[0]) if numbers else None
    except ValueError:
        return None


def passes_salary_filter(salary_text, min_salary, max_salary):
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
def is_duplicate(seen, new_job):
    return any(
        j.get("title")        == new_job.get("title")
        and j.get("company")  == new_job.get("company")
        and j.get("posting_date") == new_job.get("posting_date")
        for j in seen
    )


# ── DOM helpers ───────────────────────────────────────────────────────────────
def get_text(element, selector):
    el = element.query_selector(selector)
    return el.inner_text().strip() if el else "N/A"


# ── Core scraper ──────────────────────────────────────────────────────────────
def scrape_indeed(
    role: str,
    location: str,
    min_salary: Optional[int] = None,
    max_salary: Optional[int] = None,
) -> list:
    role_slug     = role.strip().lower().replace(" ", "+")
    location_slug = location.strip().lower().replace(" ", "+")
    is_remote     = location.strip().lower() in ("remote", "work-from-home", "wfh")
    is_any_mode   = role.strip().lower() in ("any", "all", "") and location.strip().lower() in ("any", "all", "")

    if is_any_mode:
        # Indeed has no true "browse all" page — use a broad query sorted by
        # newest first across all of India, no specific role keyword
        url = "https://in.indeed.com/jobs?l=India&sort=date"
    elif is_remote:
        url = f"https://in.indeed.com/jobs?q={role_slug}&sc=0kf%3Aattr%28DSQF7%29%3B&sort=date"
    else:
        url = f"https://in.indeed.com/jobs?q={role_slug}&l={location_slug}&sort=date"

    print(f"\n[SEARCH] Opening: {url}")
    scraped = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
            ]
        )

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )

        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-IN', 'en'] });
            window.chrome = { runtime: {} };
        """)

        page = context.new_page()

        if STEALTH_AVAILABLE:
            stealth_sync(page)
            print("[STEALTH] Stealth mode active")

        try:
            page.goto(url, timeout=60_000, wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
        except Exception as e:
            msg = f"[Indeed] Failed to load {url}: {e}"
            logging.error(msg)
            print(f"[ERROR] Could not load page: {e}")
            alert_slack(f"[ERROR] Indeed scraper failed\n{msg}")
            browser.close()
            return []

        # Indeed job cards
        cards = page.query_selector_all("div.job_seen_beacon, div.jobsearch-ResultsList > li")
        print(f"[FOUND] Found {len(cards)} listings\n")

        if not cards:
            # Try extracting from embedded JSON as fallback
            content = page.content()
            pattern = r'window\.mosaic\.providerData\["mosaic-provider-jobcards"\]\s*=\s*(\{.*?\});'
            match   = re.search(pattern, content, re.DOTALL)
            if match:
                try:
                    data     = json.loads(match.group(1))
                    job_list = data.get("metaData", {}).get("mosaicProviderJobCardsModel", {}).get("results", [])
                    print(f"[FOUND] Found {len(job_list)} listings via JSON\n")
                    for job in job_list:
                        try:
                            title      = job.get("title", "N/A")
                            company    = job.get("company", "N/A")
                            location_t = job.get("formattedLocation", "N/A").lower()
                            salary     = job.get("salarySnippet", {}).get("text", "Not disclosed") or "Not disclosed"
                            date_raw   = job.get("formattedRelativeTime", "N/A")
                            job_key    = job.get("jobkey", "")
                            apply_link = f"https://in.indeed.com/viewjob?jk={job_key}" if job_key else "N/A"
                            job_type   = ", ".join(job.get("jobTypes", [])) or "N/A"

                            if not passes_salary_filter(salary, min_salary, max_salary):
                                continue

                            record = {
                                "title": title, "company": company,
                                "location": location_t, "job_type": job_type,
                                "salary": salary, "salary_numeric": parse_salary_amount(salary),
                                "skills_required": [], "posting_date": date_raw,
                                "apply_link": apply_link, "source_platform": SOURCE_PLATFORM,
                                "scraped_at": datetime.utcnow().isoformat() + "Z",
                            }
                            if not is_duplicate(scraped, record):
                                scraped.append(record)
                                print(f"  [OK] {title} — {company}")
                        except Exception as e:
                            logging.error(f"[Indeed] JSON job error: {e}")
                except Exception as e:
                    logging.error(f"[Indeed] JSON parse error: {e}")
            else:
                msg = f"[Indeed] No listings found at {url}"
                logging.error(msg)
                alert_slack(f"[WARN] Indeed 0 listings\n{url}")

            browser.close()
            return scraped

        # Parse DOM cards
        for card in cards:
            try:
                title_el   = card.query_selector("h2.jobTitle span, a.jcs-JobTitle span")
                title      = title_el.inner_text().strip() if title_el else "N/A"

                company_el = card.query_selector("span.companyName, [data-testid='company-name']")
                company    = company_el.inner_text().strip() if company_el else "N/A"

                loc_el     = card.query_selector("div.companyLocation, [data-testid='text-location']")
                location_t = loc_el.inner_text().strip().lower() if loc_el else "N/A"

                sal_el     = card.query_selector("div.metadata.salary-snippet-container, [data-testid='attribute_snippet_testid']")
                salary     = sal_el.inner_text().strip() if sal_el else "Not disclosed"

                date_el    = card.query_selector("span.date, [data-testid='myJobsStateDate']")
                date_raw   = date_el.inner_text().strip() if date_el else "N/A"

                link_el    = card.query_selector("h2.jobTitle a, a.jcs-JobTitle")
                href       = link_el.get_attribute("href") if link_el else ""
                apply_link = ("https://in.indeed.com" + href) if href and href.startswith("/") else href or "N/A"

                if not passes_salary_filter(salary, min_salary, max_salary):
                    print(f"  [FILTERED] Filtered: {title}")
                    continue

                record = {
                    "title":           title,
                    "company":         company,
                    "location":        location_t,
                    "job_type":        "N/A",
                    "salary":          salary,
                    "salary_numeric":  parse_salary_amount(salary),
                    "skills_required": [],
                    "posting_date":    date_raw,
                    "apply_link":      apply_link,
                    "source_platform": SOURCE_PLATFORM,
                    "scraped_at":      datetime.utcnow().isoformat() + "Z",
                }

                if not is_duplicate(scraped, record):
                    scraped.append(record)
                    sal = f"Rs {record['salary_numeric']:,}" if record["salary_numeric"] else record["salary"]
                    print(f"  [OK] {title} — {company} | {sal}")

            except Exception as e:
                logging.error(f"[Indeed] Card error: {e}")
                print(f"  [WARN]  Skipped one card (logged)")
                continue

        browser.close()

    return scraped


# ── Save to disk ──────────────────────────────────────────────────────────────
def save_jobs(jobs, filepath):
    with open(filepath, "w") as f:
        json.dump(jobs, f, indent=2)
    print(f"\n[SAVED] Saved {len(jobs)} jobs -> {filepath}")


# ── Push to server ────────────────────────────────────────────────────────────
def push_to_server(jobs, api_url=SERVER_URL):
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
        except requests.exceptions.ConnectionError:
            print("\n[ERROR] Cannot connect to server at http://localhost:3000")
            break
        except Exception as e:
            failed += 1
            logging.error(f"Push error: {e}")
    print(f"\n[STATS] Server push: {pushed} stored, {skipped} duplicates, {failed} failed")


# ── Salary prompt ─────────────────────────────────────────────────────────────
def prompt_salary_filter():
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
            return None

    return (
        parse_int("  Min salary Rs/year (blank = no minimum): "),
        parse_int("  Max salary Rs/year (blank = no maximum): "),
    )


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    role         = input("Enter role (e.g. software engineer, marketing): ").strip()
    location     = input("Enter location (e.g. bangalore, remote):        ").strip()
    min_s, max_s = prompt_salary_filter()

    jobs = scrape_indeed(role, location, min_salary=min_s, max_salary=max_s)

    if jobs:
        save_jobs(jobs, OUTPUT_FILE)
        push_to_server(jobs)
    else:
        print("\n[WARN]  No jobs found matching your filters.")
        alert_slack(f"[WARN] Indeed 0 results for '{role}' in '{location}'")
