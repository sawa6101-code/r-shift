import os
import re
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from icalendar import Calendar, Event

BASE_URL = os.getenv("RSHIFT_BASE_URL", "https://sgy5zm.rshift.jp").rstrip("/")
LOGIN_URL = os.getenv("RSHIFT_LOGIN_URL", "").strip()
STAFF_PAGE_URL = os.getenv("RSHIFT_STAFF_PAGE_URL", "").strip()
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
    m = re.search(r"(\d{1,2})[:：](\d{2})", value)
    if not m:
        raise ValueError(f"時刻を解析できません: {value}")
    return int(m.group(1)), int(m.group(2))


def parse_shift_rows(html):
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


def find_login_form(response):
    soup = BeautifulSoup(response.text, "html.parser")
    form = soup.find("form")
    if not form:
        return None, None
    action = form.get("action") or response.url
    return form, urljoin(response.url, action)


def login_and_fetch():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
        "Accept-Language": "ja,en;q=0.8",
    })

    # R-ShiftのログインURLは企業・環境ごとに異なるため、固定の
    # /staff/login/ を前提にせず、まずテナントURL自身へアクセスして
    # リダイレクト先のログインフォームを自動検出する。
    candidates = []
    if LOGIN_URL:
        candidates.append(LOGIN_URL)
    candidates.append(BASE_URL + "/")
    candidates.extend([
        BASE_URL + "/staff/",
        BASE_URL + "/staffpage/",
        BASE_URL + "/login/",
    ])

    response = None
    form = None
    post_url = None
    errors = []
    seen = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        try:
            r = s.get(url, timeout=30, allow_redirects=True)
            if r.status_code >= 400:
                errors.append(f"{url} -> HTTP {r.status_code}")
                continue
            f, action = find_login_form(r)
            # ログインフォームを見つけたら採用。フォームがない場合も、
            # 既にログイン済み画面の可能性があるため後で判定する。
            if f is not None:
                response, form, post_url = r, f, action
                break
            if "ログアウト" in r.text or "シフト" in r.text:
                response = r
                break
        except requests.RequestException as exc:
            errors.append(f"{url} -> {exc}")

    if response is None:
        raise RuntimeError("R-Shiftのログイン画面を取得できませんでした。試行先: " + "; ".join(errors))

    if form is not None:
        data = {}
        for inp in form.select("input[name]"):
            if inp.get("type", "").lower() not in {"submit", "button", "image"}:
                data[inp["name"]] = inp.get("value", "")
        id_field = os.getenv("RSHIFT_ID_FIELD", "login_id")
        password_field = os.getenv("RSHIFT_PASSWORD_FIELD", "password")
        data[id_field] = USER_ID
        data[password_field] = PASSWORD
        r = s.post(post_url, data=data, timeout=30, allow_redirects=True)
        r.raise_for_status()
        if "ログイン" in r.text and "パスワード" in r.text and "ログアウト" not in r.text:
            raise RuntimeError("R-Shiftへのログインに失敗した可能性があります。ID/パスワードまたはフォーム項目を確認してください。")
        response = r

    # 明示されたスタッフページがあれば優先。未指定ならログイン後のURLを起点に探索。
    targets = []
    if STAFF_PAGE_URL:
        targets.append(STAFF_PAGE_URL)
    targets.extend([response.url, BASE_URL + "/staffpage/", BASE_URL + "/staff/"])
    for target in dict.fromkeys(targets):
        try:
            r = s.get(target, timeout=30, allow_redirects=True)
            if r.status_code < 400 and ("ログイン" not in r.text or "ログアウト" in r.text):
                return r.text
        except requests.RequestException:
            continue
    raise RuntimeError("ログイン後のスタッフページを取得できませんでした。")


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
        raise RuntimeError("シフトを0件取得しました。ログインは通過しましたが、実サイトのHTML構造に合わせてシフト抽出処理を調整する必要があります。")
    cal = build_calendar(shifts)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(cal.to_ical())
    print(f"{len(shifts)}件のシフトを {OUTPUT} に出力しました。")


if __name__ == "__main__":
    main()
