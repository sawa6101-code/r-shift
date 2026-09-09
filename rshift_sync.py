import os
import re
import hashlib
import time
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
HOME_ADDRESS = os.getenv("RSHIFT_HOME_ADDRESS", "").strip()
OUTPUT = Path(os.getenv("OUTPUT_ICS_PATH", "docs/shift_calendar.ics"))
JST = timezone(timedelta(hours=9))
TIME_RE = re.compile(r"(?:[01]?\d|2[0-3])[:：][0-5]\d")
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
GEOCODE_CACHE = {}
LAST_GEOCODE_REQUEST = 0.0


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


def normalize_shop_name(text):
    text = re.sub(r"\s+", " ", text.strip())
    if not text:
        return None
    parts = text.split(" ")
    if len(parts) % 2 == 0:
        half = len(parts) // 2
        if parts[:half] == parts[half:]:
            return " ".join(parts[:half])
    return text


def extract_help_shop(node):
    """R-Shiftの応援セルから応援先店舗名を取得する。"""
    for shop_node in node.select(".help_shop"):
        text = normalize_shop_name(shop_node.get_text(" ", strip=True))
        if text:
            return text
        for attr in ("data-shop-name", "data-store-name", "title", "aria-label"):
            value = normalize_shop_name(shop_node.get(attr, ""))
            if value:
                return value
    for attr in ("data-shop-name", "data-store-name", "data-help-shop", "title", "aria-label"):
        value = normalize_shop_name(node.get(attr, ""))
        if value:
            return value
    text = node.get_text(" ", strip=True)
    text = TIME_RE.sub(" ", text)
    text = text.replace("応援", " ")
    return normalize_shop_name(text)


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
        if "holiday_shift" in classes or "working_shift" not in classes:
            continue
        d = date(year, month, idx + 1)

        if "help_shift" in classes:
            shop = extract_help_shop(node)
            summary = f"{shop}応援" if shop else "応援"
            start = datetime(d.year, d.month, d.day, 9, 0, tzinfo=JST)
            end = datetime(d.year, d.month, d.day, 20, 0, tzinfo=JST)
            item = (start, end, summary, shop, True)
        else:
            times = TIME_RE.findall(node.get_text(" ", strip=True))
            if len(times) < 2:
                continue
            shift = make_shift(d, times[:2])
            if not shift:
                continue
            item = (shift[0], shift[1], "店舗勤務", None, False)

        key = (item[0], item[1], item[2], item[3])
        if key not in seen:
            seen.add(key)
            shifts.append(item)

    return sorted(shifts, key=lambda item: item[0])


def geocode(query):
    """Nominatimを利用したジオコード。結果を実行中キャッシュし、最低1秒間隔を守る。"""
    global LAST_GEOCODE_REQUEST
    if query in GEOCODE_CACHE:
        return GEOCODE_CACHE[query]

    wait = 1.05 - (time.monotonic() - LAST_GEOCODE_REQUEST)
    if wait > 0:
        time.sleep(wait)

    response = requests.get(
        NOMINATIM_URL,
        params={"q": query, "format": "json", "limit": 1, "countrycodes": "jp"},
        headers={"User-Agent": "r-shift-sync/1.1 (GitHub Actions)"},
        timeout=30,
    )
    LAST_GEOCODE_REQUEST = time.monotonic()
    response.raise_for_status()
    results = response.json()
    coords = None
    if results:
        coords = (float(results[0]["lon"]), float(results[0]["lat"]))
    GEOCODE_CACHE[query] = coords
    return coords


def get_shop_coordinates(shop):
    """店舗名から位置を取得する。店舗名だけで失敗するケースに住所検索を追加。"""
    aliases = {
        "ことぶき店": [
            "スギ薬局 ことぶき店 愛知県春日井市ことぶき町8-3",
            "愛知県春日井市ことぶき町8-3",
        ],
    }
    queries = aliases.get(shop, []) + [
        f"スギ薬局 {shop} 愛知県春日井市",
        f"スギ薬局 {shop} 愛知県",
        f"{shop} 愛知県春日井市",
        f"{shop} 愛知県",
    ]
    seen = set()
    for query in queries:
        if query in seen:
            continue
        seen.add(query)
        try:
            coords = geocode(query)
        except requests.RequestException as exc:
            print(f"店舗位置検索をスキップ: {query} ({exc})")
            continue
        if coords:
            return coords
    return None


def route_minutes(origin, destination):
    response = requests.get(
        f"https://router.project-osrm.org/route/v1/driving/{origin[0]},{origin[1]};{destination[0]},{destination[1]}",
        params={"overview": "false"},
        timeout=30,
    )
    response.raise_for_status()
    routes = response.json().get("routes", [])
    if not routes:
        return None
    return max(1, round(routes[0]["duration"] / 60))


def add_travel_events(cal, shifts):
    if not HOME_ADDRESS:
        raise RuntimeError("RSHIFT_HOME_ADDRESS が未設定です。自宅住所をGitHub Secretsに登録してください")
    try:
        origin = geocode(HOME_ADDRESS)
    except requests.RequestException as exc:
        print(f"自宅位置検索に失敗したため移動イベントを省略: {exc}")
        return
    if not origin:
        print("自宅住所を地図上の地点に変換できないため、移動イベントを省略します")
        return

    for start, end, summary, location, is_help in shifts:
        if not is_help or not location:
            continue
        destination = get_shop_coordinates(location)
        if not destination:
            print(f"応援先店舗の位置情報を取得できないため、移動イベントを省略: {location}")
            continue
        try:
            minutes = route_minutes(origin, destination)
        except requests.RequestException as exc:
            print(f"移動時間取得に失敗したため移動イベントを省略: {location} ({exc})")
            continue
        if minutes is None:
            print(f"応援先店舗までのルートが見つからないため、移動イベントを省略: {location}")
            continue
        departure = start - timedelta(minutes=minutes)

        event = Event()
        uid_source = f"travel|{departure.isoformat()}|{start.isoformat()}|{location}|{minutes}"
        event.add("uid", hashlib.sha256(uid_source.encode()).hexdigest() + "@r-shift-sync")
        event.add("summary", f"移動：自宅→{location}")
        event.add("dtstart", departure)
        event.add("dtend", start)
        event.add("location", location)
        event.add("X-RSHIFT-ORIGIN", "自宅")
        event.add("X-RSHIFT-DESTINATION", location)
        event.add("X-RSHIFT-TRAVEL-MINUTES", str(minutes))
        event.add("X-APPLE-TRAVEL-ADVISORY-BEHAVIOR", "AUTOMATIC")
        event.add("dtstamp", datetime.now(timezone.utc))
        cal.add_component(event)


def build_calendar(shifts):
    cal = Calendar()
    cal.add("prodid", "-//r-shift-sync//JP")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("X-WR-CALNAME", "R-Shift シフト")
    cal.add("X-WR-TIMEZONE", "Asia/Tokyo")

    for start, end, summary, location, is_help in shifts:
        event = Event()
        uid_source = f"{start.isoformat()}|{end.isoformat()}|{summary}|{location or ''}"
        event.add("uid", hashlib.sha256(uid_source.encode()).hexdigest() + "@r-shift-sync")
        event.add("summary", summary)
        event.add("dtstart", start)
        event.add("dtend", end)
        if location:
            event.add("location", location)
        event.add("dtstamp", datetime.now(timezone.utc))
        cal.add_component(event)

    add_travel_events(cal, shifts)
    return cal


def main():
    html, year, month, staff_name = fetch_staff_page()
    shifts = parse_staff_month(html, year, month, staff_name)
    if not shifts:
        raise RuntimeError("対象月の確定勤務シフトを0件取得しました")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(build_calendar(shifts).to_ical())
    help_count = sum(1 for _, _, summary, _, is_help in shifts if is_help)
    print(f"{len(shifts)}件のシフト（うち応援{help_count}件）を {OUTPUT} に出力しました。")


if __name__ == "__main__":
    main()
