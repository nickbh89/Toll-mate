#!/usr/bin/env python3
"""
Toll Mate — Weekly Database Updater
Scrapes official UK government sources for new/changed toll roads and zones,
compares against the Google Sheet, and updates it automatically.
Runs every Monday at 08:00 UTC via GitHub Actions.
"""

import requests
import csv
import io
import json
import re
import os
from datetime import datetime
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials

# ── CONFIG ────────────────────────────────────────────────────────────────────
SHEET_ID = '1Ml0QS6EYeexJxkcmtHMLcNdHlN4HQPp0BKnFzrpghFs'
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

# Official sources to scrape
SOURCES = [
    {
        'name': 'GOV.UK Toll Roads',
        'url': 'https://www.gov.uk/uk-toll-roads',
        'type': 'gov'
    },
    {
        'name': 'GOV.UK Clean Air Zones',
        'url': 'https://www.gov.uk/clean-air-zones',
        'type': 'gov'
    },
    {
        'name': 'Transport Scotland LEZ',
        'url': 'https://www.transport.gov.scot/our-approach/environment/low-emission-zones/',
        'type': 'gov'
    },
]

# Known toll roads — used to detect genuinely new ones
KNOWN_NAMES = [
    'mersey gateway', 'silver jubilee', 'dartford', 'dart charge',
    'tyne tunnel', 'blackwall tunnel', 'silvertown tunnel',
    'm6 toll', 'durham road user', 'london ulez', 'london congestion',
    'london lez', 'oxford zez', 'bath caz', 'birmingham caz',
    'bradford caz', 'bristol caz', 'portsmouth caz', 'sheffield caz',
    'tyneside caz', 'glasgow lez', 'edinburgh lez', 'aberdeen lez',
    'dundee lez'
]

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; TollMateBot/1.0; +https://github.com/nickbh89/Toll-mate)'
}

def get_sheet_client():
    """Authenticate with Google Sheets using service account."""
    creds_json = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
    if not creds_json:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON secret not set")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)

def get_current_db(client):
    """Fetch current database from Google Sheet."""
    sh = client.open_by_key(SHEET_ID)
    ws = sh.sheet1
    rows = ws.get_all_records()
    return rows, ws, sh

def scrape_source(source):
    """Scrape an official source and extract toll road mentions."""
    try:
        res = requests.get(source['url'], headers=HEADERS, timeout=15)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, 'html.parser')
        text = soup.get_text(separator=' ', strip=True).lower()
        return text
    except Exception as e:
        print(f"  Warning: Could not scrape {source['name']}: {e}")
        return ''

def check_for_new_tolls(page_texts, current_rows):
    """
    Look for mentions of toll roads not in our database.
    Returns list of potential new entries to flag for review.
    """
    current_names = [r['name'].lower() for r in current_rows]
    new_found = []

    # Patterns that suggest a new toll/zone
    patterns = [
        r'new\s+(?:toll|charge|zone|crossing)',
        r'(?:toll|charge)\s+(?:road|zone|tunnel|bridge|crossing)\s+(?:to\s+)?(?:open|launch|start|begin|introduce)',
        r'(?:anpr|barrier.free|cashless)\s+(?:toll|charge)',
        r'(?:congestion|clean air|emission|ulez|caz|lez|zez)\s+zone\s+(?:launch|open|start|new|expand)',
    ]

    alerts = []
    for source_name, text in page_texts.items():
        for pattern in patterns:
            matches = re.findall(r'.{0,60}' + pattern + r'.{0,60}', text, re.IGNORECASE)
            for match in matches:
                match_lower = match.lower()
                # Check if it mentions something we don't know about
                is_known = any(known in match_lower for known in KNOWN_NAMES)
                if not is_known and len(match.strip()) > 20:
                    alerts.append({
                        'source': source_name,
                        'text': match.strip()[:200],
                        'pattern': pattern
                    })

    return alerts

def verify_urls(current_rows):
    """Check all payment URLs are still returning 200."""
    broken = []
    checked = set()
    for row in current_rows:
        url = row.get('url', '').strip()
        if not url or url in checked:
            continue
        checked.add(url)
        try:
            res = requests.head(url, headers=HEADERS, timeout=10, allow_redirects=True)
            if res.status_code >= 400:
                broken.append({'name': row['name'], 'url': url, 'status': res.status_code})
                print(f"  ⚠ Broken URL ({res.status_code}): {row['name']} — {url}")
            else:
                print(f"  ✓ OK ({res.status_code}): {row['name']}")
        except Exception as e:
            broken.append({'name': row['name'], 'url': url, 'status': f'ERROR: {e}'})
            print(f"  ✗ Error: {row['name']} — {e}")
    return broken

def update_last_verified(ws, current_rows):
    """Update the last_verified column for all active rows."""
    today = datetime.now().strftime('%Y-%m')
    # Find last_verified column index (column L = 12)
    all_values = ws.get_all_values()
    headers = all_values[0]
    try:
        lv_col = headers.index('last_verified') + 1
        # Update each data row
        for i, row in enumerate(current_rows, start=2):
            if row.get('active', 'TRUE') == 'TRUE':
                ws.update_cell(i, lv_col, today)
    except (ValueError, Exception) as e:
        print(f"  Warning: Could not update last_verified: {e}")

def log_update(sh, message, source='auto-update'):
    """Write to the update_log sheet."""
    try:
        try:
            log_ws = sh.worksheet('update_log')
        except:
            log_ws = sh.add_worksheet('update_log', rows=1000, cols=3)
            log_ws.update('A1:C1', [['timestamp', 'change_description', 'source']])
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M UTC')
        log_ws.append_row([timestamp, message, source])
    except Exception as e:
        print(f"  Warning: Could not write to log: {e}")

def main():
    print("=" * 60)
    print(f"Toll Mate Database Updater — {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    # 1. Connect to Google Sheet
    print("\n1. Connecting to Google Sheet...")
    try:
        client = get_sheet_client()
        current_rows, ws, sh = get_current_db(client)
        print(f"   ✓ Connected — {len(current_rows)} rows found")
    except Exception as e:
        print(f"   ✗ Failed to connect: {e}")
        return

    # 2. Verify all payment URLs
    print("\n2. Verifying payment URLs...")
    broken_urls = verify_urls(current_rows)
    if broken_urls:
        msg = f"⚠ {len(broken_urls)} broken URL(s) detected: " + ", ".join([b['name'] for b in broken_urls])
        print(f"   {msg}")
        log_update(sh, msg, 'url-checker')
    else:
        print("   ✓ All URLs responding correctly")
        log_update(sh, f"✓ URL check passed — all {len(current_rows)} entries verified", 'url-checker')

    # 3. Scrape official sources for new toll roads
    print("\n3. Scraping official UK sources for new toll roads...")
    page_texts = {}
    for source in SOURCES:
        print(f"   Checking {source['name']}...")
        text = scrape_source(source)
        if text:
            page_texts[source['name']] = text
            print(f"   ✓ Scraped {len(text)} chars")

    # 4. Check for new tolls
    print("\n4. Analysing for new or changed toll roads...")
    alerts = check_for_new_tolls(page_texts, current_rows)
    if alerts:
        print(f"   ⚠ {len(alerts)} potential change(s) detected — logging for review")
        for alert in alerts[:5]:  # Log first 5
            msg = f"⚠ Potential new toll detected from {alert['source']}: {alert['text'][:150]}"
            log_update(sh, msg, 'scraper')
    else:
        print("   ✓ No new toll roads detected")
        log_update(sh, "✓ Scrape complete — no new toll roads or zones detected", 'scraper')

    # 5. Update last_verified timestamps
    print("\n5. Updating verification timestamps...")
    try:
        update_last_verified(ws, current_rows)
        print(f"   ✓ Updated last_verified for all active rows")
    except Exception as e:
        print(f"   Warning: {e}")

    # 6. Summary
    print("\n" + "=" * 60)
    print("Update complete.")
    print(f"  Rows in database: {len(current_rows)}")
    print(f"  Broken URLs: {len(broken_urls)}")
    print(f"  New toll alerts: {len(alerts)}")
    if broken_urls:
        print("\n  ⚠ ACTION NEEDED: Review broken URLs in update_log sheet")
        print(f"  Sheet: https://docs.google.com/spreadsheets/d/{SHEET_ID}")
    print("=" * 60)

if __name__ == '__main__':
    main()
