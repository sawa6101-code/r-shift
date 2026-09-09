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
        dates = re.findall(r"20\d{2}[/-]\d{1,2}[/-]\d{1,2}", text)
        times = re.findall(r"\d{1,2}[:：]\d{2}", text)
        if not dates:
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
    forms = soup.find_all("form")
    for form in forms:
        inputs = form.select("input[name]")
        if any(i.get("type", "").lower() == "password" for i in inputs):
            action = form.get("action") or response.url
            return form, urljoin(response.url, action)
    if forms:
        form = forms[0]
        action = form.get("action") or response.url
        return form, urljoin(response.url, action)
    return None, None


def detect_login_fields(form):
    inputs = form.select("input[name]")
    password = next((i["name"] for i in inputs if i.get("type", "").lower() == "password"), None)
    if not password:
        password = os.getenv("RSHIFT_PASSWORD_FIELD", "password")

    id_candidates = ("login_id", "userid", "user_id", "username", "login", "staff_id", "id")
    user = None
    for name in id_candidates:
        if any(i.get("name") == name for i in inputs):
            user = name
            break
    if not user:
        text_inputs = [i for i in inputs if i.get("type", "text").lower() in ("text", "email")]
        user = text_inputs[0].get("name") if text_inputs else os.getenv("RSHIFT_ID_FIELD", "login_id")
    return user, password


def login_and_fetch():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
        "Accept-Language": "ja,en;q=0.8",
    })

    # transactionid はログイン後に発行されるセッション情報なので固定値として保存しない。
    # テナントURLから実際のログインフォームを自動検出する。
    candidates = []
    if LOGIN_URL:
        candidates.append(LOGIN_URL)
    candidates.extend([
        BASE_URL + "/",
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
            typ = inp.get("type", "").lower()
            if typ not in {"submit", "button", "image", "password"}:
                data[inp["name"]] = inp.get("value", "")
        id_field, password_field = detect_login_fields(form)
        data[id_field] = USER_ID
        data[password_field] = PASSWORD
        r = s.post(post_url, data=data, timeout=30, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        still_login = bool(soup.find("input", {"type": "password"}))
        if still_login and "ログアウト" not in r.text:
            raise RuntimeError("R-Shiftへのログインに失敗した可能性があります。ログインフォームの認証結果を確認してください。")
        response = r

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
        raise RuntimeError("ログイン後のページからシフトを0件取得しました。実際のシフト表HTMLに合わせて抽出処理を調整する必要があります。")
    cal = build_calendar(shifts)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(cal.to_ical())
    print(f"{len(shifts)}件のシフトを {OUTPUT} に出力しました。")


if __name__ == "__main__":
    main()
