import html
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

JOBS_URL = "https://freelanceyard.com/en/jobs"
SEEN_FILE = Path("seen_jobs.json")
MAX_SEEN_ITEMS = 300
MAX_FIRST_RUN_SEND = int(os.getenv("MAX_FIRST_RUN_SEND", "3"))
REQUEST_TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    "Referer": "https://freelanceyard.com/en/jobs",
}


def clean_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def clean_multiline(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines()]
    non_empty = [line for line in lines if line]
    return "\n".join(non_empty)


def load_seen() -> list[str]:
    if not SEEN_FILE.exists():
        return []
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(x) for x in data]
    except Exception as exc:
        print(f"[WARN] Could not read {SEEN_FILE}: {exc}")
    return []


def save_seen(urls: list[str]) -> None:
    trimmed = urls[-MAX_SEEN_ITEMS:]
    SEEN_FILE.write_text(
        json.dumps(trimmed, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def fetch_html(session: requests.Session, url: str) -> str | None:
    try:
        resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        print(f"[ERROR] Network error for {url}: {exc}")
        return None

    if resp.status_code in (403, 429):
        print(f"[STOP] Received HTTP {resp.status_code} for {url}. Stopping safely to avoid rate limits.")
        return "RATE_LIMITED"

    if resp.status_code != 200:
        print(f"[WARN] Unexpected HTTP {resp.status_code} for {url}")
        return None

    return resp.text


def parse_jobs_list(page_html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(page_html, "html.parser")
    cards = soup.select("div.h-full.p-4.mb-4.bg-white.border.rounded-lg")
    jobs: list[dict[str, Any]] = []

    for card in cards:
        title_link = card.select_one("h3 a")
        if not title_link or not title_link.get("href"):
            continue

        url = title_link["href"].strip()
        title = clean_spaces(title_link.get_text())

        category_el = card.select_one("div.text-stone-500")
        category = clean_spaces(category_el.get_text()) if category_el else "غير محدد"

        budget_el = card.select_one("span.text-green-700")
        budget = clean_spaces(budget_el.get_text()) if budget_el else "غير محدد"
        budget = re.sub(r"^Budget:\s*", "", budget, flags=re.IGNORECASE)

        client = "غير محدد"
        user_icon = card.select_one("i.uil-user")
        if user_icon and user_icon.parent:
            client = clean_spaces(user_icon.parent.get_text())

        posted_el = card.select_one("div.text-sm.text-gray-400")
        posted_at = clean_spaces(posted_el.get_text()) if posted_el else "غير محدد"
        posted_at = re.sub(r"^Posted at:\s*", "", posted_at, flags=re.IGNORECASE)

        jobs.append(
            {
                "url": url,
                "title": title,
                "category": category,
                "budget": budget,
                "client": client,
                "posted_at": posted_at,
            }
        )

    return jobs


def parse_job_details(detail_html: str, base_job: dict[str, Any]) -> dict[str, Any]:
    soup = BeautifulSoup(detail_html, "html.parser")
    job = dict(base_job)
    job.setdefault("description", "")
    job.setdefault("milestones", [])
    job.setdefault("skills", [])
    job.setdefault("deadline", "غير محدد")

    header = soup.select_one("#job-header")
    if header:
        h3 = header.select_one("h3")
        if h3:
            job["title"] = clean_spaces(h3.get_text())
        h4 = header.select_one("h4")
        if h4:
            job["client"] = clean_spaces(h4.get_text())
        posted_el = header.select_one("div.text-sm.text-gray-400")
        if posted_el:
            posted_txt = clean_spaces(posted_el.get_text())
            job["posted_at"] = re.sub(r"^Posted at:\s*", "", posted_txt, flags=re.IGNORECASE)

    details = soup.select_one("#job-details")
    if not details:
        return job

    desc_el = details.select_one("div.break-words.text-muted")
    if desc_el:
        job["description"] = clean_multiline(desc_el.get_text())

    for section in details.find_all("div", class_="mb-4", recursive=False):
        heading_el = section.find("h5")
        if not heading_el:
            continue
        heading = clean_spaces(heading_el.get_text()).lower()

        # Explicitly ignore Attached documents (never scrape or download files)
        if "attached document" in heading:
            continue

        if "milestone" in heading:
            milestones = []
            for li in section.select("ul li"):
                m_title_el = li.find("h5")
                m_desc_el = li.select_one("div.break-normal")
                m_title = clean_spaces(m_title_el.get_text()) if m_title_el else ""
                m_desc = clean_spaces(m_desc_el.get_text()) if m_desc_el else ""
                if m_title or m_desc:
                    milestones.append({"title": m_title, "description": m_desc})
            job["milestones"] = milestones

        elif "required skill" in heading:
            skills = [
                clean_spaces(span.get_text())
                for span in section.select("span")
                if clean_spaces(span.get_text())
            ]
            job["skills"] = skills

        elif "deadline" in heading:
            span = section.find("span")
            raw = clean_spaces(span.get_text() if span else section.get_text())
            deadline = re.sub(r"^Deadline\s*:?\s*", "", raw, flags=re.IGNORECASE).strip()
            if deadline:
                job["deadline"] = deadline

        elif "client budget" in heading or "budget" in heading:
            span = section.find("span")
            raw = clean_spaces(span.get_text() if span else section.get_text())
            budget = re.sub(r"^(Client\s+)?Budget\s*:?\s*", "", raw, flags=re.IGNORECASE).strip()
            if budget:
                job["budget"] = budget

    return job


def format_telegram_message(job: dict[str, Any]) -> str:
    esc = html.escape
    parts = [
        f"🆕 <b>{esc(job['title'])}</b>",
        "",
        f"📂 <b>التصنيف:</b> {esc(job.get('category', 'غير محدد'))}",
        f"👤 <b>العميل:</b> {esc(job.get('client', 'غير محدد'))}",
        f"💰 <b>الميزانية:</b> {esc(job.get('budget', 'غير محدد'))}",
        f"📅 <b>تاريخ النشر:</b> {esc(job.get('posted_at', 'غير محدد'))}",
        f"⏳ <b>آخر موعد (Deadline):</b> {esc(job.get('deadline', 'غير محدد'))}",
    ]

    description = (job.get("description") or "").strip()
    if description:
        if len(description) > 1800:
            description = description[:1800].rstrip() + "..."
        parts.append("")
        parts.append("📝 <b>التفاصيل:</b>")
        parts.append(esc(description))

    milestones = job.get("milestones") or []
    if milestones:
        parts.append("")
        parts.append("✅ <b>المراحل (Milestones):</b>")
        for m in milestones[:5]:
            m_title = esc(m.get("title", ""))
            m_desc = esc(m.get("description", ""))
            if m_title and m_desc:
                parts.append(f"• <b>{m_title}:</b> {m_desc}")
            elif m_title:
                parts.append(f"• <b>{m_title}</b>")
            elif m_desc:
                parts.append(f"• {m_desc}")

    skills = job.get("skills") or []
    if skills:
        parts.append("")
        parts.append("🏷️ <b>المهارات المطلوبة:</b>")
        parts.append(esc(" • ".join(skills)))

    msg = "\n".join(parts)
    if len(msg) > 4000:
        msg = msg[:3995].rstrip() + "..."
    return msg


def send_telegram_message(token: str, chat_id: str, job: dict[str, Any]) -> bool:
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": format_telegram_message(job),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "🔗 فتح الوظيفة على Freelance Yard", "url": job["url"]}]
            ]
        },
    }
    try:
        resp = requests.post(api_url, json=payload, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            return True
        print(f"[ERROR] Telegram API error {resp.status_code}: {resp.text}")
        return False
    except requests.RequestException as exc:
        print(f"[ERROR] Telegram send failed: {exc}")
        return False


def main() -> int:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        print("[ERROR] Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variables.")
        return 1

    seen_list = load_seen()
    seen_set = set(seen_list)
    is_first_run = len(seen_list) == 0

    session = requests.Session()
    list_html = fetch_html(session, JOBS_URL)
    if list_html == "RATE_LIMITED" or list_html is None:
        return 0

    jobs = parse_jobs_list(list_html)
    print(f"[INFO] Found {len(jobs)} jobs on page 1.")

    new_jobs = [job for job in jobs if job["url"] not in seen_set]
    if not new_jobs:
        print("[INFO] No new jobs found.")
        return 0

    # On the very first run, send only the newest N jobs as a live test and mark the rest as seen
    if is_first_run and len(new_jobs) > MAX_FIRST_RUN_SEND:
        skipped = new_jobs[MAX_FIRST_RUN_SEND:]
        for old_job in skipped:
            seen_list.append(old_job["url"])
            seen_set.add(old_job["url"])
        new_jobs = new_jobs[:MAX_FIRST_RUN_SEND]
        print(f"[INFO] First run: sending latest {len(new_jobs)} jobs and marking {len(skipped)} older ones as seen.")

    # Reverse so older new jobs arrive first and the very newest arrives last
    new_jobs.reverse()

    sent_count = 0
    for job in new_jobs:
        time.sleep(random.uniform(2.0, 3.5))
        detail_html = fetch_html(session, job["url"])
        if detail_html == "RATE_LIMITED":
            break

        full_job = parse_job_details(detail_html, job) if detail_html else job
        if send_telegram_message(bot_token, chat_id, full_job):
            seen_list.append(job["url"])
            seen_set.add(job["url"])
            sent_count += 1
            print(f"[OK] Sent: {full_job['title']}")
            time.sleep(1.5)

    save_seen(seen_list)
    print(f"[DONE] Sent {sent_count} new job(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
