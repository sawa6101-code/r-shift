import os
import re
import hashlib
from calendar import monthrange
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


def parse_yyyymmdd(value):
    value = clean(value)
    if re.fullmatch(r"20\d{6}", value):
        return datetime.strptime(value, "%Y%m%d").date()
    return None


def make_shift(d, fh, fm, th, tm):
    try:
        start = datetime(d.year, d.month, d.day, int(fh), int(fm), tzinfo=JST)
        end = datetime(d.year, d.month, d.day, int(th), int(tm), tzinfo=JST)
    except (TypeError, ValueError):
        return None
    if end <= start:
        end += timedelta(days=1)
    return start, end


def target_month():
    value = os.getenv("RSHIFT_TARGET_MONTH", "").strip()
    if value:
        m = re.fullmatch(r"(20\d{2})-(0[1-9]|1[0-2])", value)
        if not m:
            raise RuntimeError("RSHIFT_TARGET_MONTH は YYYY-MM 形式で指定してください")
        return int(m.group(1)), int(m.group(2))
    now = datetime.now(JST)
    return now.year, now.month


def find_login_form(response):
    soup = BeautifulSoup(response.text, "html.parser")
    for form in soup.find_all("form"):
        if any(i.get("type", "").lower() == "password" for i in form.select("input[name]")):
            return form, urljoin(response.url, form.get("action") or response.url)
    return None, None


def detect_login_fields(form):
    inputs = form.select("input[name]")
    password = next((i["name"] for i in inputs if i.get("type", "").lower() == "password"), "password")
    candidates = ("login_id", "userid", "user_id", "username", "login", "staff_id", "id")
    user = next((n for n in candidates if any(i.get("name") == n for i in inputs)), None)
    if not user:
        text_inputs = [i for i in inputs if i.get("type", "text").lower() in ("text", "email")]
        user = text_inputs[0].get("name") if text_inputs else os.getenv("RSHIFT_ID_FIELD", "login_id")
    return user, password


def login_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
        "Accept-Language": "ja,en;q=0.8",
    })
    candidates = [u for u in (LOGIN_URL, BASE_URL + "/", BASE_URL + "/staff/", BASE_URL + "/staffpage/", BASE_URL + "/login/") if u]
    response = None
    form = None
    post_url = None
    for url in dict.fromkeys(candidates):
        try:
            r = s.get(url, timeout=30, allow_redirects=True)
            if r.status_code >= 400:
                continue
            f, action = find_login_form(r)
            if f is not None:
                response, form, post_url = r, f, action
                break
            if "ログアウト" in r.text or "シフト" in r.text:
                response = r
                break
        except requests.RequestException:
            continue
    if response is None:
        raise RuntimeError("R-Shiftのログイン画面を取得できませんでした")

    if form is not None:
        data = {}
        for inp in form.select("input[name]"):
            typ = inp.get("type", "").lower()
            if typ not in {"submit", "button", "image", "password"}:
                data[inp["name"]] = inp.get("value", "")
        user_field, password_field = detect_login_fields(form)
        data[user_field] = USER_ID
        data[password_field] = PASSWORD
        r = s.post(post_url, data=data, timeout=30, allow_redirects=True)
        r.raise_for_status()
        if BeautifulSoup(r.text, "html.parser").find("input", {"type": "password"}) and "ログアウト" not in r.text:
            raise RuntimeError("R-Shiftへのログインに失敗した可能性があります")
        response = r
    return s, response


def fetch_staff_page():
    s, response = login_session()
    targets = []
    if STAFF_PAGE_URL:
        targets.append(STAFF_PAGE_URL)
    targets.extend([response.url, BASE_URL + "/staffpage/", BASE_URL + "/staff/"])
    page = None
    for target in dict.fromkeys(targets):
        try:
            r = s.get(target, timeout=30, allow_redirects=True)
            if r.status_code < 400 and ("ログイン" not in r.text or "ログアウト" in r.text):
                page = r
                break
        except requests.RequestException:
            continue
    if page is None:
        raise RuntimeError("ログイン後のスタッフページを取得できませんでした")

    year, month = target_month()
    first = f"{year:04d}{month:02d}01"
    last = f"{year:04d}{month:02d}{monthrange(year, month)[1]:02d}"
    soup = BeautifulSoup(page.text, "html.parser")
    monthly_form = next((f for f in soup.find_all("form") if "/staffpage/monthly.php" in (f.get("action") or "")), None)
    if monthly_form is None:
        raise RuntimeError("R-Shift月間シフトフォームを見つけられませんでした")

    data = {}
    for inp in monthly_form.select("input[name]"):
        typ = inp.get("type", "").lower()
        if typ not in {"submit", "button", "image"}:
            data[inp["name"]] = inp.get("value", "")
    data["mode"] = data.get("mode") or "monthly"
    data["target_date_from"] = first
    data["target_date_to"] = last

    action = urljoin(page.url, monthly_form.get("action") or "/staffpage/monthly.php")
    r = s.post(action, data=data, timeout=30, allow_redirects=True)
    r.raise_for_status()
    if "ログイン" in r.text and "ログアウト" not in r.text:
        raise RuntimeError("対象月の月間シフトページ取得時にログインへ戻されました")
    print(f"R-Shift対象月: {year:04d}-{month:02d}")
    return r.text


def parse_rshift_hidden_data(html):
    soup = BeautifulSoup(html, "html.parser")
    shifts = []
    seen = set()
    for form in soup.find_all("form"):
        buttons = form.select("button.plan_list_shift")
        if not buttons:
            continue
        for idx, button in enumerate(buttons):
            classes = set(button.get("class", []))
            if "plan_update_color" not in classes:
                continue
            def val(name):
                node = form.select_one(f'input[name="{name}{idx}"]')
                return node.get("value", "") if node else ""
            d = parse_yyyymmdd(val("select_date"))
            if not d:
                continue
            fh, fm, th, tm = val("from_hour"), val("from_minutes"), val("to_hour"), val("to_minutes")
            if not all(v != "" for v in (fh, fm, th, tm)):
                continue
            shift = make_shift(d, fh, fm, th, tm)
            if shift and shift not in seen:
                seen.add(shift)
                shifts.append(shift)
    return sorted(shifts)


def parse_shift_rows(html):
    shifts = parse_rshift_hidden_data(html)
    if not shifts:
        raise RuntimeError("R-Shift対象月から確定シフトを0件取得しました。ページ構造または対象月を確認してください")
    return shifts


def build_calendar(shifts):
    cal = Calendar()
    cal.add("prodid", "-//r-shift-sync//JP")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("X-WR-CALNAME", "R-Shift シフト")
    cal.add("X-WR-TIMEZONE", "Asia/Tokyo")
    for start, end in shifts:
        event = Event()
        uid = hashlib.sha256(f"{start.isoformat()}|{end.isoformat()}".encode()).hexdigest() + "@r-shift-sync"
        event.add("uid", uid)
        event.add("summary", "アールシフト（出勤）")
        event.add("dtstart", start)
        event.add("dtend", end)
        event.add("dtstamp", datetime.now(timezone.utc))
        cal.add_component(event)
    return cal


def main():
    html = fetch_staff_page()
    shifts = parse_shift_rows(html)
    cal = build_calendar(shifts)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(cal.to_ical())
    print(f"{len(shifts)}件のシフトを {OUTPUT} に出力しました。")


if __name__ == "__main__":
    main()
