import os
import re
import hashlib
from calendar import monthrange
from datetime import datetime, timezone, timedelta, date
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
HOME_LOCATION = os.getenv("RSHIFT_HOME_LOCATION", "自宅").strip() or "自宅"
JST = timezone(timedelta(hours=9))
TIME_RE = re.compile(r"(?:[01]?\d|2[0-3])[:：][0-5]\d")


def target_month():
    value = os.getenv("RSHIFT_TARGET_MONTH", "").strip()
    if value:
        m = re.fullmatch(r"(20\d{2})-(0[1-9]|1[0-2])", value)
        if not m:
            raise RuntimeError("RSHIFT_TARGET_MONTH は YYYY-MM 形式で指定してください")
        return int(m.group(1)), int(m.group(2))
    now = datetime.now(JST)
    return now.year, now.month


def make_shift(d, times):
    if d is None or len(times) < 2:
        return None
    try:
        sh, sm = map(int, re.split(r"[:：]", times[0]))
        eh, em = map(int, re.split(r"[:：]", times[1]))
        start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=JST)
        end = datetime(d.year, d.month, d.day, eh, em, tzinfo=JST)
    except (TypeError, ValueError):
        return None
    if end <= start:
        end += timedelta(days=1)
    return start, end


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
    urls = [u for u in (LOGIN_URL, BASE_URL + "/", BASE_URL + "/staff/", BASE_URL + "/staffpage/", BASE_URL + "/login/") if u]
    response = None
    form = None
    post_url = None
    for url in dict.fromkeys(urls):
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


def extract_staff_name(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["a", "span", "div", "p", "td", "th", "label"]):
        text = tag.get_text(" ", strip=True)
        if "パスワードを変更" in text and "ログアウト" in text:
            prefix = text.split("パスワードを変更", 1)[0].strip()
            if ">" in prefix:
                prefix = prefix.rsplit(">", 1)[-1].strip()
            if prefix and len(prefix) <= 40:
                return prefix
    raise RuntimeError("ログイン中の従業員名を取得できませんでした")


def fetch_staff_page():
    s, response = login_session()
    staff_name = extract_staff_name(response.text)
    targets = ([STAFF_PAGE_URL] if STAFF_PAGE_URL else []) + [response.url, BASE_URL + "/staffpage/", BASE_URL + "/staff/"]
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
    data["target_date_from"] = first
    data["target_date_to"] = last
    action = urljoin(page.url, monthly_form.get("action") or "/staffpage/monthly.php")
    r = s.post(action, data=data, timeout=30, allow_redirects=True)
    r.raise_for_status()
    if "ログイン" in r.text and "ログアウト" not in r.text:
        raise RuntimeError("対象月の月間シフトページ取得時にログインへ戻されました")
    print(f"R-Shift対象月: {year:04d}-{month:02d}")
    return r.text, year, month, staff_name


def extract_help_shop(node):
    """R-Shiftの応援セルから応援先店舗名を取得する。"""
    # R-Shiftでは応援先が help_shop 要素として表示されるため、まずそこを優先する。
    for shop_node in node.select(".help_shop"):
        text = shop_node.get_text(" ", strip=True)
        if text:
            return text
        for attr in ("data-shop-name", "data-store-name", "title", "aria-label"):
            value = shop_node.get(attr, "").strip()
            if value:
                return value

    # HTMLの属性として店舗名を持つ実装差にも対応する。
    for attr in ("data-shop-name", "data-store-name", "data-help-shop", "title", "aria-label"):
        value = node.get(attr, "").strip()
        if value:
            return value

    # 最終フォールバック。時刻文字列と「応援」表記を除いて店舗名らしい文字列を抽出する。
    text = node.get_text(" ", strip=True)
    text = TIME_RE.sub(" ", text)
    text = re.sub(r"\b応援\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def parse_staff_month(html, year, month, staff_name):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.staffpage-monthly-table")
    if table is None:
        raise RuntimeError("月間シフト表を見つけられませんでした")
    target_row = None
    for row in table.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if cells and staff_name in cells[0].get_text(" ", strip=True):
            target_row = row
            break
    if target_row is None:
        raise RuntimeError("ログイン中の従業員の月間シフト行を特定できませんでした")
    cells = target_row.find_all(["td", "th"])
    if len(cells) < 2:
        raise RuntimeError("従業員シフト行の構造を認識できませんでした")
    shift_cols = cells[1].select(".staff_row.shift_col")
    days = monthrange(year, month)[1]
    if len(shift_cols) < days:
        raise RuntimeError(f"月間シフト列数が不足しています: {len(shift_cols)} / {days}")
    shifts = []
    seen = set()
    for idx, node in enumerate(shift_cols[:days]):
        classes = set(node.get("class", []))
        if "holiday_shift" in classes:
            continue
        if "working_shift" not in classes:
            continue
        times = TIME_RE.findall(node.get_text(" ", strip=True))
        if len(times) < 2:
            continue
        d = date(year, month, idx + 1)
        shift = make_shift(d, times[:2])
        if not shift:
            continue

        if "help_shift" in classes:
            shop = extract_help_shop(node)
            summary = f"応援：{shop}" if shop else "応援"
            location = shop or "応援先"
        else:
            summary = "アールシフト（出勤）"
            location = None

        item = (shift[0], shift[1], summary, location)
        key = (shift[0], shift[1], summary, location)
        if key not in seen:
            seen.add(key)
            shifts.append(item)
    return sorted(shifts, key=lambda x: x[0])


def build_calendar(shifts):
    cal = Calendar()
    cal.add("prodid", "-//r-shift-sync//JP")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("X-WR-CALNAME", "R-Shift シフト")
    cal.add("X-WR-TIMEZONE", "Asia/Tokyo")
    for start, end, summary, location in shifts:
        event = Event()
        uid_source = f"{start.isoformat()}|{end.isoformat()}|{summary}|{location or ''}"
        event.add("uid", hashlib.sha256(uid_source.encode()).hexdigest() + "@r-shift-sync")
        event.add("summary", summary)
        event.add("dtstart", start)
        event.add("dtend", end)
        event.add("dtstamp", datetime.now(timezone.utc))
        if location:
            # カレンダー上の目的地は応援先店舗。iOS等で場所として扱えるようLOCATIONに設定。
            event.add("location", location)
            # iCalendar標準では出発地点をイベントごとに保持する項目がないため、固定出発地点を拡張属性として保存。
            event.add("X-RSHIFT-ORIGIN", HOME_LOCATION)
            event.add("X-RSHIFT-DESTINATION", location)
            event.add("X-APPLE-TRAVEL-ADVISORY-BEHAVIOR", "AUTOMATIC")
        cal.add_component(event)
    return cal


def main():
    html, year, month, staff_name = fetch_staff_page()
    shifts = parse_staff_month(html, year, month, staff_name)
    if not shifts:
        raise RuntimeError("対象月の確定勤務シフトを0件取得しました")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(build_calendar(shifts).to_ical())
    help_count = sum(1 for _, _, summary, _ in shifts if summary.startswith("応援"))
    print(f"{len(shifts)}件のシフト（うち応援{help_count}件）を {OUTPUT} に出力しました。")


if __name__ == "__main__":
    main()
