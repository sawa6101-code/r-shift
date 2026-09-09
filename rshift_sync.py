import os
import re
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

TIME_RE = re.compile(r"(?:[01]?\d|2[0-3])[:：][0-5]\d")
DATE_PATTERNS = (
    r"20\d{2}[/-]\d{1,2}[/-]\d{1,2}",
    r"20\d{2}年\d{1,2}月\d{1,2}日",
    r"\d{1,2}[/-]\d{1,2}",
    r"\d{1,2}月\d{1,2}日",
)


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def parse_date(value, default_year=None, default_month=None):
    value = clean(value)
    year = default_year or datetime.now(JST).year

    m = re.search(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})", value)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()

    m = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", value)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()

    m = re.search(r"(\d{1,2})[/-](\d{1,2})", value)
    if m:
        return datetime(year, int(m.group(1)), int(m.group(2))).date()

    m = re.search(r"(\d{1,2})月(\d{1,2})日", value)
    if m:
        return datetime(year, int(m.group(1)), int(m.group(2))).date()

    m = re.search(r"(\d{1,2})日", value)
    if m and default_month:
        return datetime(year, default_month, int(m.group(1))).date()

    raise ValueError(f"日付を解析できません: {value}")


def extract_date(text, default_year, default_month):
    text = clean(text)
    for pattern in DATE_PATTERNS:
        m = re.search(pattern, text)
        if m:
            try:
                return parse_date(m.group(0), default_year, default_month)
            except ValueError:
                pass
    return None


def parse_time(value):
    value = clean(value).replace("時", ":").replace("分", "")
    m = TIME_RE.search(value)
    if not m:
        raise ValueError(f"時刻を解析できません: {value}")
    hour, minute = map(int, re.split(r"[:：]", m.group(0)))
    return hour, minute


def node_metadata(node):
    values = [node.get_text(" ", strip=True)]
    for key, value in node.attrs.items():
        if key == "class":
            continue
        if isinstance(value, dict):
            values.extend(str(v) for v in value.values())
        elif isinstance(value, str):
            values.append(value)
    return clean(" | ".join(values))


def explicit_date_candidates(soup, default_year, default_month):
    """ページ全体から日付ラベルを収集する。月セレクタ等のページ全体テキストは参照しない。"""
    tags = soup.find_all(True)
    index = {id(tag): i for i, tag in enumerate(tags)}
    candidates = []
    seen = set()
    for tag in tags:
        # 日付ラベルは葉要素を優先し、親要素に複数の日付が含まれる場合の誤認を防ぐ。
        if tag.find(True):
            continue
        d = extract_date(node_metadata(tag), default_year, default_month)
        if d and d not in seen:
            seen.add(d)
            candidates.append((index[id(tag)], tag, d))
    return candidates, index


def date_from_related_structure(node, default_year, default_month):
    """同じ行・カード・セル群に属する明示的な日付を優先して取得する。"""
    # まず同一テーブル行を確認する。
    tr = node.find_parent("tr")
    if tr:
        found = []
        for tag in tr.find_all(True):
            if tag.find(True):
                continue
            d = extract_date(node_metadata(tag), default_year, default_month)
            if d and d not in found:
                found.append(d)
        if len(found) == 1:
            return found[0]

    # 次に近い祖先カードを確認する。複数日付を含む大きなコンテナは除外する。
    current = node
    for _ in range(8):
        if current is None:
            break
        found = []
        for tag in current.find_all(True):
            if tag.find(True):
                continue
            d = extract_date(node_metadata(tag), default_year, default_month)
            if d and d not in found:
                found.append(d)
                if len(found) > 1:
                    break
        if len(found) == 1:
            return found[0]
        current = current.parent
    return None


def nearest_date(node, candidates, index, default_year, default_month):
    d = date_from_related_structure(node, default_year, default_month)
    if d:
        return d

    # DOM上で最も近い明示的日付ラベルを使う。
    pos = index.get(id(node))
    if pos is None or not candidates:
        return None
    return min(candidates, key=lambda item: abs(item[0] - pos))[2]


def make_shift(d, times):
    if d is None or len(times) < 2:
        return None
    try:
        sh, sm = parse_time(times[0])
        eh, em = parse_time(times[1])
    except ValueError:
        return None
    start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=JST)
    end = datetime(d.year, d.month, d.day, eh, em, tzinfo=JST)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def parse_rshift_dom(soup, page_year, page_month):
    selectors = [
        ".staffpage-plan-list-shift",
        ".plan_list_shift",
        ".popup_working_time",
        ".shift_non_confirm",
    ]
    nodes = []
    seen_nodes = set()
    for selector in selectors:
        for node in soup.select(selector):
            if id(node) not in seen_nodes:
                seen_nodes.add(id(node))
                nodes.append(node)

    date_candidates, index = explicit_date_candidates(soup, page_year, page_month)
    shifts = []
    seen_shift = set()

    for node in nodes:
        time_values = []
        for tn in node.select(".popup_working_time"):
            time_values.extend(TIME_RE.findall(node_metadata(tn)))
        if not time_values:
            time_values = TIME_RE.findall(node_metadata(node))
        if len(time_values) < 2:
            continue

        d = nearest_date(node, date_candidates, index, page_year, page_month)
        shift = make_shift(d, time_values[:2])
        if shift and shift not in seen_shift:
            seen_shift.add(shift)
            shifts.append(shift)

    return shifts


def parse_generic_dom(soup, page_year, page_month):
    selectors = [
        "tr.shift-row", "table tr", "li", "div[class*='shift']",
        "div[class*='schedule']", "div[class*='work']", "td", "th",
    ]
    date_candidates, index = explicit_date_candidates(soup, page_year, page_month)
    shifts = []
    seen_shift = set()
    seen_text = set()

    for selector in selectors:
        for node in soup.select(selector):
            text = clean(node.get_text(" ", strip=True))
            if not text or text in seen_text:
                continue
            seen_text.add(text)
            times = TIME_RE.findall(text)
            if len(times) < 2:
                continue
            d = nearest_date(node, date_candidates, index, page_year, page_month) or extract_date(text, page_year, page_month)
            shift = make_shift(d, times[:2])
            if shift and shift not in seen_shift:
                seen_shift.add(shift)
                shifts.append(shift)
    return shifts


def parse_shift_rows(html):
    soup = BeautifulSoup(html, "html.parser")
    now = datetime.now(JST)

    # ページ全体の最初の「○月」は使用しない。隠し月セレクタ等で別月を拾う問題を防ぐ。
    full_dates = []
    for tag in soup.find_all(True):
        if tag.find(True):
            continue
        text = node_metadata(tag)
        if re.search(r"20\d{2}年|\d{1,2}[/-]\d{1,2}|\d{1,2}月\d{1,2}日", text):
            full_dates.append(text)

    year_match = re.search(r"(20\d{2})年", " ".join(full_dates))
    page_year = int(year_match.group(1)) if year_match else now.year
    md_matches = re.findall(r"(\d{1,2})[/-]\d{1,2}|(\d{1,2})月\d{1,2}日", " ".join(full_dates))
    page_month = now.month
    if md_matches:
        page_month = int(next(a or b for a, b in md_matches))

    shifts = parse_rshift_dom(soup, page_year, page_month)
    if not shifts:
        shifts = parse_generic_dom(soup, page_year, page_month)

    # 明らかな誤取得（全件が同一日なのに複数の明示日付がある）を検知する。
    date_candidates, _ = explicit_date_candidates(soup, page_year, page_month)
    unique_days = {start.date() for start, _ in shifts}
    if len(shifts) >= 2 and len(date_candidates) >= 2 and len(unique_days) == 1:
        raise RuntimeError("R-Shiftの日付関連付けが不自然です。複数の日付候補があるのに取得シフトが1日に集中したため、誤ったカレンダー生成を停止しました。")

    shifts.sort()
    return shifts


def print_page_diagnostics(html):
    soup = BeautifulSoup(html, "html.parser")
    print("[diagnostic] title:", clean(soup.title.get_text()) if soup.title else "(none)")
    print("[diagnostic] forms:", len(soup.find_all("form")), "tables:", len(soup.find_all("table")))
    for selector in (".staffpage-plan-list-shift", ".plan_list_shift", ".popup_working_time", ".shift_non_confirm"):
        matches = soup.select(selector)
        print(f"[diagnostic] {selector}: {len(matches)}")
        for node in matches[:8]:
            times = TIME_RE.findall(node_metadata(node))
            print(f"[diagnostic]   tag={node.name} children={len(node.find_all(True))} times={len(times)} classes={node.get('class', [])}")


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
    user = next((name for name in id_candidates if any(i.get("name") == name for i in inputs)), None)
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
            raise RuntimeError("R-Shiftへのログインに失敗した可能性があります。")
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
        event.add("uid", hashlib.sha256(f"{start.isoformat()}|{end.isoformat()}".encode()).hexdigest() + "@r-shift-sync")
        event.add("summary", "アールシフト（出勤）")
        event.add("dtstart", start)
        event.add("dtend", end)
        event.add("dtstamp", datetime.now(timezone.utc))
        cal.add_component(event)
    return cal


def main():
    html = login_and_fetch()
    try:
        shifts = parse_shift_rows(html)
    except RuntimeError:
        print_page_diagnostics(html)
        raise
    if not shifts:
        print_page_diagnostics(html)
        raise RuntimeError("ログイン後のページからシフトを0件取得しました。")

    cal = build_calendar(shifts)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(cal.to_ical())
    print(f"{len(shifts)}件のシフトを {OUTPUT} に出力しました。")


if __name__ == "__main__":
    main()
