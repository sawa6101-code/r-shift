import os
import re
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from icalendar import Calendar, Event

BASE_URL = os.getenv("RSHIFT_BASE_URL", "https://sgy5zm.rshift.jp").rstrip("/")
LOGIN_URL = os.getenv("RSHIFT_LOGIN_URL", f"{BASE_URL}/staff/login/")
STAFF_PAGE_URL = os.getenv("RSHIFT_STAFF_PAGE_URL", f"{BASE_URL}/staffpage/")
USER_ID = os.environ["RSHIFT_USER_ID"]
PASSWORD = os.environ["RSHIFT_PASSWORD"]
OUTPUT = Path(os.getenv("OUTPUT_ICS_PATH", "docs/shift_calendar.ics"))
JST = timezone(timedelta(hours=9))


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def parse_date(value):
    value = clean(value).replace("年", "-").replace("月", "-").replace("日", "")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    m = re.search(r"(20\d{2})[-/]?(\d{1,2})[-/]?(\d{1,2})", value)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
    raise ValueError(f"日付を解析できません: {value}")


def parse_time(value):
    value = clean(value).replace("時", ":").replace("分", "")
    m = re.search(r"(\d{1,2}):(\d{2})", value)
    if not m:
        m = re.search(r"(\d{1,2})[：](\d{2})", value)
    if not m:
        raise ValueError(f"時刻を解析できません: {value}")
    return int(m.group(1)), int(m.group(2))


def parse_shift_rows(html):
    """R-ShiftのHTML差異に耐える汎用パーサー。

    最優先で class='shift-row' のセルを読み、見つからない場合は
    テーブルの行をテキストとして解析します。実サイトのHTMLが確認できれば
    SELECTORSを固定化するのが最も確実です。
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("tr.shift-row") or soup.select("table tr")
    shifts = []

    for row in rows:
        cells = [clean(c.get_text(" ", strip=True)) for c in row.select("th,td")]
        if len(cells) < 2:
            continue
        text = " | ".join(cells)
        if any(x in text for x in ("日付", "曜日", "合計", "勤務時間")) and not re.search(r"20\d{2}", text):
            continue
        dates = re.findall(r"20\d{2}[/-]\d{1,2}[/-]\d{1,2}", text)
        times = re.findall(r"\d{1,2}[:：]\d{2}", text)
        if not dates or len(times) < 2:
            # 日本語日付表記にも対応
            m = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", text)
            if m:
                dates = [f"{m.group(1)}-{m.group(2)}-{m.group(3)}"]
        if not dates or len(times) < 2:
            continue
        try:
            d = parse_date(dates[0])
            sh, sm = parse_time(times[0])
            eh, em = parse_time(times[1])
        except ValueError:
            continue
        start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=JST)
        end = datetime(d.year, d.month, d.day, eh, em, tzinfo=JST)
        if end <= start:
            end += timedelta(days=1)
        shifts.append((start, end))
    return shifts


def login_and_fetch():
    s = requests.Session()
    s.headers.update({"User-Agent": "r-shift-sync/1.0"})
    r = s.get(LOGIN_URL, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    form = soup.find("form")
    data = {}
    if form:
        for inp in form.select("input[name]"):
            if inp.get("type") not in {"submit", "button"}:
                data[inp["name"]] = inp.get("value", "")
    # サンプルサイトで一般的な名前。実HTMLに合わせて環境変数で上書き可能。
    data[os.getenv("RSHIFT_ID_FIELD", "login_id")] = USER_ID
    data[os.getenv("RSHIFT_PASSWORD_FIELD", "password")] = PASSWORD
    post_url = requests.compat.urljoin(LOGIN_URL, form.get("action")) if form and form.get("action") else LOGIN_URL
    r = s.post(post_url, data=data, timeout=30, allow_redirects=True)
    r.raise_for_status()
    if "ログイン" in r.text and "パスワード" in r.text and "ログアウト" not in r.text:
        raise RuntimeError("R-Shiftへのログインに失敗した可能性があります。フォーム項目を確認してください。")
    r = s.get(STAFF_PAGE_URL, timeout=30)
    r.raise_for_status()
    return r.text


def build_calendar(shifts):
    cal = Calendar()
    cal.add("prodid", "-//r-shift-sync//JP")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("X-WR-CALNAME", "R-Shift シフト")
    cal.add("X-WR-TIMEZONE", "Asia/Tokyo")
    for start, end in shifts:
        event = Event()
        uid = hashlib.sha256(f"rshift:{start.isoformat()}:{end.isoformat()}".encode()).hexdigest() + "@r-shift-sync"
        event.add("uid", uid)
        event.add("summary", "アールシフト（出勤）")
        event.add("dtstart", start)
        event.add("dtend", end)
        event.add("dtstamp", datetime.now(timezone.utc))
        cal.add_component(event)
    return cal


def main():
    html = login_and_fetch()
    shifts = parse_shift_rows(html)
    if not shifts:
        raise RuntimeError("シフトを0件取得しました。安全のため既存ICSは上書きしません。実サイトのHTMLに合わせてパーサーを調整してください。")
    cal = build_calendar(shifts)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(cal.to_ical())
    print(f"{len(shifts)}件のシフトを {OUTPUT} に出力しました。")


if __name__ == "__main__":
    main()
