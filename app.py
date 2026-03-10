import streamlit as st
import pandas as pd
import json
import os
import re
import time
import calendar
import zipfile
import io
import xml.etree.ElementTree as ET
import requests
from datetime import datetime, date

# ─────────────────────────────────────────────
# 페이지 설정
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="주주총회 일정 트래커",
    page_icon="📅",
    layout="wide",
    initial_sidebar_state="expanded",
)

EXCEL_PATH = "주주총회.xlsx"
STATE_PATH = "agm_state.json"
CORP_CACHE = "dart_corp_codes.json"

# ─────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;600;700&display=swap');
* { font-family: 'Noto Sans KR', sans-serif; box-sizing: border-box; }

@keyframes pulse-gold {
  0%,100% { background:#ffe066; } 50% { background:#fff3a0; }
}
.updated-badge {
  display:inline-block; background:#ffe066; color:#7a5800;
  font-weight:700; font-size:.75em; padding:1px 7px; border-radius:10px;
  margin-left:5px; border:1px solid #f5c518;
  animation: pulse-gold 1.1s ease-in-out 5;
}

/* ─── 달력 ─── */
.cal-wrap { overflow-x: auto; }
table.cal {
  width: 100%; border-collapse: collapse; table-layout: fixed;
}
table.cal th {
  background: #1e3a5f; color: #fff; text-align: center;
  padding: 8px 4px; font-size: .82em; font-weight: 600;
}
table.cal th.week-col { background: #0f2540; font-size: .78em; }
table.cal td {
  vertical-align: top; border: 1px solid #dde3ed;
  padding: 5px 5px 8px 5px; background: #fff;
  font-size: .8em; width: 13%;
}
table.cal td.week-total {
  background: #f0f4fa; text-align: center; vertical-align: middle;
  font-weight: 700; color: #1e3a5f; font-size: .85em; width: 4%;
  border: 1px solid #c5d0e0;
}
table.cal td.empty { background: #f8f9fc; }
table.cal td.today { background: #fffbe6; border: 2px solid #f5c518; }
table.cal td.weekend { background: #fafafa; }

.cal-day-num {
  font-weight: 700; font-size: .9em; color: #374151; margin-bottom: 4px;
  display: flex; align-items: center; gap: 4px;
}
.day-badge {
  background: #1e3a5f; color: #fff; font-size: .7em; font-weight: 700;
  border-radius: 8px; padding: 1px 6px; min-width: 22px; text-align: center;
}
.day-badge.has-pending { background: #b45309; }

.chip {
  display: inline-block; border-radius: 10px; padding: 2px 7px;
  margin: 2px 2px 0 0; font-size: .73em; font-weight: 500; line-height: 1.6;
  cursor: default; max-width: 100%; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap;
}
.chip-confirmed        { background: #dcfce7; color: #166534; border: 1px solid #86efac; }
.chip-confirmed.req    { background: #dbeafe; color: #1e40af; border: 1px solid #93c5fd; }
.chip-updated          { background: #fef9c3; color: #854d0e; border: 1.5px solid #fde047; }
.chip-pending          { background: #fff7ed; color: #9a3412; border: 1px dashed #fdba74; font-style: italic; }
.chip-pending.req      { background: #fef3c7; color: #92400e; border: 1px dashed #fcd34d; }

.week-cnt  { font-size: 1.15em; }
.week-sub  { font-size: .68em; color: #64748b; margin-top: 3px; }

/* ─── 리스트 뷰 ─── */
.date-conf { color: #166534; font-weight: 600; }
.date-pend { color: #9a3412;  font-style: italic; }
.dart-ok   { background: #dcfce7; color: #166534; font-size: .78em; padding: 2px 8px; border-radius: 8px; font-weight: 600; }
.dart-same { color: #6b7280; font-size: .78em; }
.dart-err  { color: #dc2626; font-size: .78em; }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────
# 데이터 로드 / 상태 관리
# ─────────────────────────────────────────────

@st.cache_data
def load_excel_data():
    df = pd.read_excel(EXCEL_PATH, header=0, usecols="B:D")
    df.columns = ["단체명", "주주총회일", "비고"]
    df = df.dropna(subset=["단체명"]).reset_index(drop=True)

    def fmt(d):
        if isinstance(d, (datetime, date)):
            return d.strftime("%Y-%m-%d")
        return str(d).strip() if pd.notna(d) and d else ""

    df["주주총회일"] = df["주주총회일"].apply(fmt)
    df["비고"] = df["비고"].fillna("")
    return df


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        raw["updated_recently"] = set(raw.get("updated_recently", []))
        return raw
    return {"overrides": {}, "changes": {}, "updated_recently": set(),
            "updated_timestamps": {}, "name_replacements": {}}


def save_state(state):
    out = dict(state)
    out["updated_recently"] = list(state.get("updated_recently", set()))
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


def init_session():
    for key, val in [
        ("state",          load_state()),
        ("change_modal",   None),
        ("inline_change",  None),   # 인라인 기업변경: company name or None
        ("expanded_prev",  set()),
        ("crawl_results",  {}),
        ("search_log",     None),   # 검색 완료 로그 {updated:[..], unchanged:[..]}
    ]:
        if key not in st.session_state:
            st.session_state[key] = val


# ─────────────────────────────────────────────
# 날짜 유틸
# ─────────────────────────────────────────────

def is_confirmed(s: str) -> bool:
    return bool(re.match(r"\d{4}-\d{2}-\d{2}", str(s)))


def extract_pending_date(date_str: str, target_year: int = 2026) -> str | None:
    """'미정 (25.3.20)' → '2026-03-20'"""
    m = re.search(r"(\d{1,2})\.(\d{1,2})", str(date_str))
    if m:
        return f"{target_year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


def get_display_date(company: str, orig: str, state: dict) -> str:
    return state["overrides"].get(company, orig)


# ─────────────────────────────────────────────
# 공통 HTTP 헬퍼
# ─────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
}

def validate_march_2026(d: str) -> bool:
    """2026-03-DD 형식인지 확인"""
    return bool(d and re.match(r"2026-03-\d{2}$", d))


# ─────────────────────────────────────────────
# ① DART OpenAPI  (document.xml ZIP 방식)
# ─────────────────────────────────────────────

def load_corp_codes(api_key: str) -> dict:
    if os.path.exists(CORP_CACHE):
        if time.time() - os.path.getmtime(CORP_CACHE) < 86400:
            with open(CORP_CACHE, "r", encoding="utf-8") as f:
                return json.load(f)

    resp = requests.get(
        f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={api_key}",
        timeout=30)
    resp.raise_for_status()

    root = ET.fromstring(
        zipfile.ZipFile(io.BytesIO(resp.content)).read("CORPCODE.xml"))

    corp_dict = {}
    for item in root.findall("list"):
        name  = item.findtext("corp_name",  "").strip()
        code  = item.findtext("corp_code",  "").strip()
        stock = item.findtext("stock_code", "").strip()
        if name and code and stock:
            corp_dict[name] = code

    with open(CORP_CACHE, "w", encoding="utf-8") as f:
        json.dump(corp_dict, f, ensure_ascii=False)
    return corp_dict


def find_corp_code(corp_dict: dict, name: str) -> str | None:
    if name in corp_dict:
        return corp_dict[name]
    low = name.lower()
    for k, v in corp_dict.items():
        if k.lower() == low:
            return v
    cands = [(k, v) for k, v in corp_dict.items() if name in k or k in name]
    if cands:
        cands.sort(key=lambda x: abs(len(x[0]) - len(name)))
        return cands[0][1]
    return None


def parse_agm_date_from_xml(api_key: str, rcept_no: str) -> str | None:
    """
    강화된 정규식으로 주주총회 확정일 추출.
    태그 기반 파싱 제거, 원문 텍스트 전체에 넓은 패턴 적용.
    """
    try:
        resp = requests.get(
            "https://opendart.fss.or.kr/api/document.xml",
            params={"crtfc_key": api_key,
                    "rcept_no": rcept_no.replace("-", "")},
            timeout=20)
        resp.raise_for_status()

        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        xml_names = [f for f in zf.namelist() if f.lower().endswith(".xml")]
        if not xml_names:
            return None

        raw = zf.read(xml_names[0]).decode("utf-8", errors="ignore")

        # 패턴1: "주주총회/정기총회/개최일 ... 2026년 3월 19일" (코나아이 등 실제 문구)
        p1 = re.compile(
            r"(?:주주총회|정기총회|소집일|개최일|총회일)[^0-9]{0,30}?"
            r"(\d{4})[년.\s\-]*(\d{1,2})[월.\s\-]*(\d{1,2})",
            re.IGNORECASE
        )
        # 패턴2: "2026. 3. 19." 뒤에 총회/소집/개최
        p2 = re.compile(
            r"(\d{4})[.\-년\s]+(\d{1,2})[.\-월\s]+(\d{1,2})"
            r"[^0-9]{0,30}(?:주주총회|정기총회|소집|개최)",
            re.IGNORECASE
        )
        # 패턴3: "2026.03.19" / "2026-03-19" 순수 날짜
        p3 = re.compile(
            r"\b(2026)[.\-](0?[1-9]|1[0-2])[.\-](0?[1-9]|[12]\d|3[01])\b"
        )

        for pat in [p1, p2, p3]:
            for m in pat.finditer(raw):
                y, mo, d = m.group(1), m.group(2), m.group(3)
                date_str = f"{y}-{int(mo):02d}-{int(d):02d}"
                if validate_march_2026(date_str):
                    return date_str

        # 최후 수단: "2026" + "3" + 일자가 근접한 경우
        m = re.search(r"2026[.\s\-년]*0?3[.\s\-월]*([0-2]?\d|3[01])[.\s일]*", raw)
        if m:
            candidate = f"2026-03-{int(m.group(1)):02d}"
            if validate_march_2026(candidate):
                return candidate

    except Exception:
        pass
    return None



def search_dart_api(company_name: str, api_key: str) -> tuple[str | None, str]:
    """
    ① corp_code 조회
    ② list.json에서 '주주총회소집결의' report_nm 검색
    ③ document.xml ZIP 다운로드 → XML 태그 파싱
    """
    if not api_key:
        return None, "API 키 없음"

    year = datetime.now().year

    try:
        corp_dict = load_corp_codes(api_key)
    except Exception as e:
        return None, f"기업코드 로드 실패: {e}"

    corp_code = find_corp_code(corp_dict, company_name)
    if not corp_code:
        return None, f"기업코드 미발견 ('{company_name}')"

    try:
        resp = requests.get(
            "https://opendart.fss.or.kr/api/list.json",
            params={
                "crtfc_key":      api_key,
                "corp_code":      corp_code,
                "bgn_de":         f"{year}0101",
                "end_de":         f"{year}0331",  # 3월까지
                "last_report_at": "N",             # 모든 공시 포함 (최종보고서 한정 X)
                "page_no":        "1",
                "page_count":     "100",
            },
            timeout=12,
        )
        data = resp.json()

        if data.get("status") != "000":
            return None, f"DART 오류: {data.get('message', '')}"

        items = data.get("list", [])

        # 1순위: 소집결의
        rcept_no = report_nm = rcept_dt = ""
        for item in items:
            if "주주총회소집결의" in item.get("report_nm", ""):
                rcept_no  = item["rcept_no"]
                report_nm = item["report_nm"]
                rcept_dt  = item.get("rcept_dt", "")
                break

        # 2순위: 소집공고
        if not rcept_no:
            for item in items:
                if "주주총회소집공고" in item.get("report_nm", ""):
                    rcept_no  = item["rcept_no"]
                    report_nm = item["report_nm"]
                    rcept_dt  = item.get("rcept_dt", "")
                    break

        # 3순위: 주주총회/정기총회/소집 포함 모든 공시
        if not rcept_no:
            for item in items:
                nm = item.get("report_nm", "")
                if any(kw in nm for kw in ["주주총회", "정기총회", "소집결의", "소집공고"]):
                    rcept_no  = item["rcept_no"]
                    report_nm = nm
                    rcept_dt  = item.get("rcept_dt", "")
                    break
        if not rcept_no:
            return None, f"주주총회 공시 없음 (corp_code: {corp_code})"

        # document.xml에서 실제 주주총회 개최일 파싱
        agm_date = parse_agm_date_from_xml(api_key, rcept_no)

        if not agm_date:
            return None, f"공시 본문 날짜 파싱 실패 ({report_nm})"

        # 2026년 3월인지 검증 — 이외 값은 파싱 오류로 처리
        if not validate_march_2026(agm_date):
            return None, f"날짜 오류: {agm_date} (2026년 3월 아님)"

        return agm_date, f"DART ({report_nm})"

    except requests.exceptions.ConnectionError:
        return None, "네트워크 오류"
    except Exception as e:
        return None, f"오류: {e}"


# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# ② K-Vote 크롤링
# ─────────────────────────────────────────────

def _corp_match(query: str, candidate: str) -> bool:
    """회사명 부분 매칭 (괄호·공백·㈜ 정규화)"""
    if not query or not candidate:
        return False
    if query in candidate or candidate in query:
        return True
    def norm(s):
        return re.sub(r"[\s\(\)（）㈜()]|주식회사", "", s)
    q, c = norm(query), norm(candidate)
    return q in c or c in q


def _pick_date(text: str) -> str | None:
    """텍스트에서 2026-03-XX 추출"""
    m = re.search(
        r"2026[.\-/년\s]*0?3[.\-/월\s]*([0-2]?\d|3[01])[.\-/일\s]?",
        str(text)
    )
    if m:
        d = f"2026-03-{int(m.group(1)):02d}"
        return d if validate_march_2026(d) else None
    return None


def search_kvote(company_name: str) -> tuple[str | None, str]:
    """
    K-Vote(evote.ksd.or.kr) 에서 주주총회 일정 크롤링.
    POST → HTML table 파싱 → JSON 파싱 → GET fallback 순으로 시도.
    """
    from bs4 import BeautifulSoup

    BASE = "https://evote.ksd.or.kr"
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/json,*/*",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Referer": BASE + "/",
        "Origin": BASE,
    }

    sess = requests.Session()
    sess.headers.update(hdrs)

    # ── 방법 A: POST 검색 (회사명) ──
    for endpoint in [
        "/evote/main/agm/agmScheduleList.do",
        "/evote/main/evote/evoteScheduleList.do",
    ]:
        for payload in [
            # 형식 1
            {"agmSchdSrchTypCd": "1", "srchCrpNm": company_name,
             "agmSchdSrchYr": "2026", "pageIndex": "1", "recordCountPerPage": "100"},
            # 형식 2 (파라미터명 변형)
            {"searchType": "1", "crpNm": company_name,
             "year": "2026", "pageIndex": "1"},
        ]:
            try:
                r = sess.post(BASE + endpoint, data=payload, timeout=15)
                if r.status_code != 200:
                    continue

                # JSON 우선 시도
                try:
                    jd = r.json()
                    for lk in ("list", "data", "result", "agmList", "items", "agmSchdList"):
                        for item in jd.get(lk, []):
                            nm = str(item.get("crpNm") or item.get("corpNm") or
                                     item.get("agmCrpNm") or item.get("compNm") or "")
                            dt = str(item.get("agmDt") or item.get("agmSchdDt") or
                                     item.get("agmOpenDt") or item.get("agmDate") or "")
                            if _corp_match(company_name, nm):
                                d = _pick_date(dt) or _pick_date(str(item))
                                if d:
                                    return d, f"K-Vote ({nm})"
                except Exception:
                    pass

                # HTML 파싱
                soup = BeautifulSoup(r.text, "html.parser")
                for table in soup.find_all("table"):
                    rows = table.find_all("tr")
                    if len(rows) < 2:
                        continue
                    # 헤더로 열 위치 추정
                    headers = [th.get_text(strip=True)
                               for th in (rows[0].find_all("th") or rows[0].find_all("td"))]
                    name_idx = next((i for i, h in enumerate(headers)
                                     if any(k in h for k in ["회사", "법인", "기업", "종목"])), 0)
                    date_idx = next((i for i, h in enumerate(headers)
                                     if any(k in h for k in ["주총", "총회", "개최", "일자", "일정"])), None)
                    for tr in rows[1:]:
                        tds = tr.find_all("td")
                        if not tds:
                            continue
                        nm = tds[name_idx].get_text(strip=True) if len(tds) > name_idx else ""
                        if not _corp_match(company_name, nm):
                            continue
                        # 날짜 열 우선, 없으면 행 전체
                        dt_text = (tds[date_idx].get_text(strip=True)
                                   if date_idx and len(tds) > date_idx
                                   else tr.get_text(" ", strip=True))
                        d = _pick_date(dt_text)
                        if d:
                            return d, f"K-Vote ({nm})"

                # 페이지 전체 텍스트에서 회사 근처 날짜 탐색
                full = soup.get_text(" ")
                idx = full.find(company_name)
                if idx >= 0:
                    d = _pick_date(full[max(0, idx - 30): idx + 120])
                    if d:
                        return d, "K-Vote (텍스트)"

            except requests.exceptions.ConnectionError:
                return None, "K-Vote: 네트워크 오류"
            except Exception:
                continue

    # ── 방법 B: GET 방식 ──
    try:
        r = sess.get(
            BASE + "/evote/main/agm/agmScheduleList.do",
            params={"srchCrpNm": company_name, "agmSchdSrchYr": "2026"},
            timeout=15,
        )
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup.find_all(["tr", "li", "div"]):
                text = tag.get_text(" ", strip=True)
                if len(text) < 200 and _corp_match(company_name, text[:60]):
                    d = _pick_date(text)
                    if d:
                        return d, "K-Vote (GET)"
    except Exception:
        pass

    return None, "K-Vote: 일정 없음"


# ─────────────────────────────────────────────
# ③ Claude AI 웹 검색 (Anthropic API + web_search)
# ─────────────────────────────────────────────

def search_via_claude(company_name: str, anthropic_key: str) -> tuple[str | None, str]:
    """
    Anthropic API에 web_search 툴을 붙여서 주주총회 일자를 검색.
    채팅에서 Claude에게 묻는 것과 동일한 방식.
    """
    if not anthropic_key:
        return None, "Anthropic API 키 없음"

    prompt = (
        f"{company_name}의 2026년 정기주주총회 날짜가 언제인지 알려줘. "
        "DART 공시, K-Vote, 증권사 일정 등을 검색해서 확인해줘. "
        "날짜만 'YYYY-MM-DD' 형식으로 딱 한 줄로 답해줘. "
        "모르면 'UNKNOWN'이라고만 답해."
    )

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": anthropic_key,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "web-search-2025-03-05",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 256,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=40,
        )
        resp.raise_for_status()
        data = resp.json()

        # content 블록에서 텍스트 추출
        answer = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                answer += block.get("text", "")

        answer = answer.strip()
        if "UNKNOWN" in answer.upper() or not answer:
            return None, "AI 검색: 확인 불가"

        # YYYY-MM-DD 추출
        m = re.search(r"2026-03-(\d{2})", answer)
        if m:
            d = f"2026-03-{m.group(1)}"
            if validate_march_2026(d):
                return d, f"AI 웹검색 ({answer[:60].strip()})"

        # "3월 XX일" 형태도 처리
        d = _pick_date(answer)
        if d:
            return d, f"AI 웹검색 ({answer[:60].strip()})"

        return None, f"AI 검색: 날짜 파싱 실패 ({answer[:40]})"

    except requests.exceptions.ConnectionError:
        return None, "AI 검색: 네트워크 오류"
    except Exception as e:
        return None, f"AI 검색 오류: {str(e)[:50]}"


# ─────────────────────────────────────────────
# ④ 교차검증 통합 검색
# ─────────────────────────────────────────────

def search_agm_date(
    company_name: str,
    dart_key: str,
    anthropic_key: str = "",
) -> tuple[str | None, str, dict]:
    """
    DART → K-Vote → Claude AI 웹검색 순으로 조회 후 교차검증.
    Returns: (확정날짜 | None, 상태메시지, 상세결과dict)
    """
    detail = {
        "dart":  (None, ""),
        "kvote": (None, ""),
        "ai":    (None, ""),
    }

    # ① DART (API 키 있을 때만)
    dart_date, dart_src = (
        search_dart_api(company_name, dart_key)
        if dart_key else (None, "DART API 키 없음")
    )
    detail["dart"] = (dart_date, dart_src)

    # ② K-Vote
    kvote_date, kvote_src = search_kvote(company_name)
    detail["kvote"] = (kvote_date, kvote_src)

    # ③ AI 웹검색 (키 있을 때만, 앞 두 결과가 없을 때)
    dart_ok  = validate_march_2026(dart_date)
    kvote_ok = validate_march_2026(kvote_date)
    ai_date = ai_src = None

    if anthropic_key and not (dart_ok and kvote_ok):
        ai_date, ai_src = search_via_claude(company_name, anthropic_key)
        detail["ai"] = (ai_date, ai_src)

    ai_ok = validate_march_2026(ai_date)

    # ── 판정 ──
    dates_found = [d for d in [dart_date, kvote_date, ai_date]
                   if validate_march_2026(d)]
    sources_found = []
    if dart_ok:  sources_found.append("DART")
    if kvote_ok: sources_found.append("K-Vote")
    if ai_ok:    sources_found.append("AI검색")

    if len(dates_found) == 0:
        msgs = [s for s in [dart_src, kvote_src, ai_src] if s]
        return None, " / ".join(msgs[:2]) or "조회 실패", detail

    # 다수결: 가장 많이 나온 날짜
    from collections import Counter
    winner = Counter(dates_found).most_common(1)[0][0]

    if len(set(dates_found)) == 1:
        label = "✅ 확인 (" + "·".join(sources_found) + " 일치)"
    elif len(dates_found) >= 2 and Counter(dates_found)[winner] >= 2:
        label = f"✅ 교차확인 ({winner}, " + "·".join(sources_found) + ")"
    else:
        label = "🟡 단독확인 (" + sources_found[0] + ")"
        if len(set(dates_found)) > 1:
            label += f" ⚠️ 불일치: {dates_found}"

    return winner, label, detail

# ③ 교차검증 통합 검색
# ─────────────────────────────────────────────

def search_agm_date(company_name: str, api_key: str) -> tuple[str | None, str, dict]:
    """
    DART + K-Vote 동시 조회 후 교차검증.
    Returns: (확정날짜 | None, 상태메시지, 상세결과dict)
    """
    detail = {"dart": (None, ""), "kvote": (None, "")}

    # DART 조회
    dart_date, dart_src = search_dart_api(company_name, api_key) if api_key else (None, "API 키 없음")
    detail["dart"] = (dart_date, dart_src)

    # K-Vote 조회 (API 키 불필요)
    kvote_date, kvote_src = search_kvote(company_name)
    detail["kvote"] = (kvote_date, kvote_src)

    dart_ok  = validate_march_2026(dart_date)
    kvote_ok = validate_march_2026(kvote_date)

    # ── 교차검증 판정 ──
    if dart_ok and kvote_ok:
        if dart_date == kvote_date:
            return dart_date, f"✅ 교차확인 (DART·K-Vote 일치: {dart_date})", detail
        else:
            # 불일치 → 둘 다 표시하되 DART 우선
            return dart_date, f"⚠️ 불일치 DART={dart_date} / K-Vote={kvote_date}", detail

    if dart_ok:
        return dart_date, f"🟡 DART 단독확인 (K-Vote 미조회)", detail

    if kvote_ok:
        return kvote_date, f"🟡 K-Vote 단독확인 (DART 공시 미등록)", detail

    # 둘 다 없음
    msgs = []
    if dart_src:  msgs.append(f"DART: {dart_src}")
    if kvote_src: msgs.append(f"K-Vote: {kvote_src}")
    return None, " / ".join(msgs) or "조회 실패", detail


# ─────────────────────────────────────────────
# 달력 뷰
# ─────────────────────────────────────────────

def build_day_map(df: pd.DataFrame, state: dict) -> dict:
    day_map: dict[str, list] = {}
    for _, row in df.iterrows():
        company  = row["단체명"]
        orig     = row["주주총회일"]
        required = row["비고"] == "필수단체"
        disp     = get_display_date(company, orig, state)
        updated  = company in state.get("updated_recently", set())

        if is_confirmed(disp):
            key, confirmed = disp, True
        else:
            key = extract_pending_date(orig)
            confirmed = False

        if key:
            day_map.setdefault(key, []).append(
                {"name": company, "required": required,
                 "confirmed": confirmed, "updated": updated}
            )
    return day_map


def render_calendar_html(year: int, month: int, day_map: dict) -> str:
    # 평일(월~금)만 표시
    WEEKDAYS = ["월", "화", "수", "목", "금"]
    today_str = date.today().strftime("%Y-%m-%d")
    cal_weeks = calendar.monthcalendar(year, month)

    html = ['<div class="cal-wrap"><table class="cal"><tr>']
    for wd in WEEKDAYS:
        html.append(f"<th>{wd}</th>")
    html.append('<th class="week-col">주간<br>합계</th></tr>')

    for week in cal_weeks:
        weekdays = week[:5]  # index 0~4 = 월~금

        # 평일이 모두 0이면 행 건너뜀
        if all(d == 0 for d in weekdays):
            continue

        # 주간 합계 (평일 기준)
        wc, wp = 0, 0
        for d in weekdays:
            if d == 0:
                continue
            for item in day_map.get(f"{year}-{month:02d}-{d:02d}", []):
                if item["confirmed"]:
                    wc += 1
                else:
                    wp += 1

        html.append("<tr>")
        for d in weekdays:
            if d == 0:
                html.append('<td class="empty"></td>')
                continue

            key    = f"{year}-{month:02d}-{d:02d}"
            items  = day_map.get(key, [])
            total  = len(items)
            conf_n = sum(1 for i in items if i["confirmed"])
            pend_n = total - conf_n

            td_cls = "today" if key == today_str else ""

            badge_html = ""
            if total > 0:
                badge_cls  = "day-badge" + (" has-pending" if pend_n > 0 and conf_n == 0 else "")
                badge_html = f'<span class="{badge_cls}">{total}</span>'

            cell = (f'<td class="{td_cls}">' +
                    f'<div class="cal-day-num">{d}{badge_html}</div>')

            for item in sorted(items, key=lambda x: (not x["confirmed"], not x["required"])):
                name = item["name"]
                req  = item["required"]
                if item["updated"]:
                    cls = "chip chip-updated"
                elif item["confirmed"] and req:
                    cls = "chip chip-confirmed req"
                elif item["confirmed"]:
                    cls = "chip chip-confirmed"
                elif req:
                    cls = "chip chip-pending req"
                else:
                    cls = "chip chip-pending"

                prefix = "★" if req else ""
                suffix = "" if item["confirmed"] else " *"
                title  = name + ("" if item["confirmed"] else " (미정-작년날짜기준)")
                cell  += f'<span class="{cls}" title="{title}">{prefix}{name}{suffix}</span>'

            cell += "</td>"
            html.append(cell)

        # 주간 합계 셀
        if wc + wp > 0:
            html.append(
                f'<td class="week-total">' +
                f'<div class="week-cnt">🗓 {wc + wp}</div>' +
                f'<div class="week-sub">확정 {wc}<br>미정 {wp}</div></td>')
        else:
            html.append('<td class="week-total"><span style="color:#ccc">—</span></td>')

        html.append("</tr>")

    html.append("</table></div>")
    return "\n".join(html)



# ─────────────────────────────────────────────
# 사이드바
# ─────────────────────────────────────────────

def render_sidebar(state: dict) -> tuple[str, str]:
    st.sidebar.title("⚙️ 설정")

    # ── DART API 키 ──
    dart_api_key = st.sidebar.text_input(
        "① DART OpenAPI 키 (선택)",
        type="password",
        help="opendart.fss.or.kr 무료 발급 — 없어도 K-Vote·AI 검색 가능",
    )
    if dart_api_key:
        if os.path.exists(CORP_CACHE):
            age_h = (time.time() - os.path.getmtime(CORP_CACHE)) / 3600
            st.sidebar.caption(f"✅ 기업코드 캐시 ({age_h:.0f}시간 전)")
            if st.sidebar.button("🔄 기업코드 갱신"):
                os.remove(CORP_CACHE)
                with st.spinner("다운로드 중…"):
                    try:
                        load_corp_codes(dart_api_key)
                        st.sidebar.success("완료")
                    except Exception as e:
                        st.sidebar.error(str(e))
        else:
            st.sidebar.caption("⚠️ 첫 검색 시 자동 다운로드")

    # ── Anthropic API 키 (AI 웹검색용) ──
    st.sidebar.markdown("")
    anthropic_key = st.sidebar.text_input(
        "② Anthropic API 키 (AI 웹검색용, 선택)",
        type="password",
        help="Claude가 웹 검색으로 주총일을 직접 찾습니다. console.anthropic.com에서 발급",
    )
    if anthropic_key:
        st.sidebar.caption("✅ AI 웹검색 활성화 — DART·K-Vote 실패 시 자동 사용")
    else:
        st.sidebar.caption("💡 Anthropic 키 입력 시 웹검색으로 보완 가능")

    st.sidebar.markdown("---")

    def _run_search(corps, df, use_dart, use_anthropic, label):
        if use_dart and dart_api_key:
            try:
                with st.spinner("기업코드 로딩…"):
                    load_corp_codes(dart_api_key)
            except Exception as e:
                st.sidebar.error(str(e))
                return
        prog = st.sidebar.progress(0)
        results  = st.session_state.get("crawl_results", {})
        updated_corps   = []   # (corp, old_date, new_date, src)
        unchanged_corps = []   # (corp, date, src)
        no_result_corps = []   # corp
        for i, corp in enumerate(corps):
            prog.progress((i + 1) / len(corps), text=f"{corp}…")
            if label == "kvote":
                found, src = search_kvote(corp)
                detail_update = {"kvote": (found, src)}
            elif label == "ai":
                found, src = search_via_claude(corp, anthropic_key)
                detail_update = {"ai": (found, src)}
            else:  # full
                found, src, detail_update = search_agm_date(
                    corp, dart_api_key if use_dart else "",
                    anthropic_key if use_anthropic else "")

            existing = results.get(corp, {"date": None, "source": "", "detail": {}})
            existing["detail"] = {**existing.get("detail", {}), **detail_update}
            if validate_march_2026(found):
                r_row = df[df["단체명"] == corp]
                cur = state["overrides"].get(
                    corp, r_row.iloc[0]["주주총회일"] if not r_row.empty else "")
                if found != cur:
                    state["overrides"][corp] = found
                    state.setdefault("updated_recently", set()).add(corp)
                    state.setdefault("updated_timestamps", {})[corp] = datetime.now().isoformat()
                    updated_corps.append((corp, cur, found, src))
                else:
                    unchanged_corps.append((corp, found, src))
                existing["date"] = existing.get("date") or found
                existing["source"] = src
            else:
                no_result_corps.append((corp, src))
            results[corp] = existing
            time.sleep(0.4)
        prog.empty()
        st.session_state["crawl_results"] = results
        st.session_state["search_log"] = {
            "label":     label,
            "updated":   updated_corps,
            "unchanged": unchanged_corps,
            "no_result": no_result_corps,
            "ran_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_state(state)
        st.rerun()

    df = load_excel_data()
    corps = df["단체명"].tolist()

    # 버튼 1: 전체 교차검증 (DART + K-Vote + AI)
    if st.sidebar.button("🔍 전체 교차검증", use_container_width=True, type="primary"):
        _run_search(corps, df, bool(dart_api_key), bool(anthropic_key), "full")

    # 버튼 2: K-Vote만
    if st.sidebar.button("📋 K-Vote만 검색", use_container_width=True):
        _run_search(corps, df, False, False, "kvote")

    # 버튼 3: AI 웹검색만 (Anthropic 키 필요)
    if st.sidebar.button("🤖 AI 웹검색만", use_container_width=True,
                          disabled=not anthropic_key):
        _run_search(corps, df, False, True, "ai")

    st.sidebar.markdown("---")

    if st.sidebar.button("🗑️ 업데이트 표시 초기화", use_container_width=True):
        state["updated_recently"] = set()
        save_state(state)
        st.rerun()

    if st.sidebar.button("⚠️ 전체 초기화", use_container_width=True, type="secondary"):
        for p in [STATE_PATH, CORP_CACHE]:
            if os.path.exists(p):
                os.remove(p)
        st.session_state["state"] = load_state()
        st.rerun()

    st.sidebar.markdown("---")
    st.sidebar.markdown("""
**검색 방법 안내**

| 방법 | 키 필요 | 속도 |
|---|---|---|
| DART API | ✅ | 빠름 |
| K-Vote | ❌ | 보통 |
| AI 웹검색 | ✅ Anthropic | 느림·정확 |

**달력 범례**  
🟢 초록 = 확정 &nbsp; 🔵 파랑 = 확정+필수  
🟡 노랑 = 업데이트됨 &nbsp; 🟠 점선 = 미정  
★ = 필수단체 &nbsp; * = 미정
""")

    return dart_api_key, anthropic_key



# ─────────────────────────────────────────────
# 리스트 뷰
# ─────────────────────────────────────────────

def render_list_view(df: pd.DataFrame, state: dict, dart_api_key: str, anthropic_key: str = ""):
    overrides        = state["overrides"]
    changes          = state.get("changes", {})
    updated_recently = state.get("updated_recently", set())
    crawl_results    = st.session_state.get("crawl_results", {})

    df = df.copy()
    df["_disp"] = df.apply(lambda r: overrides.get(r["단체명"], r["주주총회일"]), axis=1)
    df["_conf"] = df["_disp"].apply(is_confirmed)

    for label, sub_df in [
        ("📅 확정", df[df["_conf"]].sort_values("_disp")),
        ("⏳ 미정", df[~df["_conf"]]),
    ]:
        if sub_df.empty:
            continue
        st.markdown(f"### {label}  <span style='font-size:.75em;color:#6b7280'>({len(sub_df)}개)</span>",
                    unsafe_allow_html=True)

        for _, row in sub_df.iterrows():
            company  = row["단체명"]
            orig     = row["주주총회일"]
            disp     = overrides.get(company, orig)
            required = row["비고"] == "필수단체"
            updated  = company in updated_recently
            has_prev = company in changes

            c1, c2, c3, c4 = st.columns([3, 2.5, 1.5, 1.8])

            with c1:
                req_sfx = " 🔴" if required else ""
                upd_html = ' <span class="updated-badge">🔄 업데이트됨</span>' if updated else ""
                if has_prev:
                    exp = company in st.session_state["expanded_prev"]
                    if st.button(f"{'▼' if exp else '▶'} {company}{req_sfx}",
                                 key=f"exp_{company}"):
                        if exp:
                            st.session_state["expanded_prev"].discard(company)
                        else:
                            st.session_state["expanded_prev"].add(company)
                        st.rerun()
                else:
                    st.markdown(
                        f'<span style="font-weight:600">{company}{req_sfx}</span>{upd_html}',
                        unsafe_allow_html=True)

            with c2:
                if is_confirmed(disp):
                    st.markdown(f'<span class="date-conf">{disp}</span>', unsafe_allow_html=True)
                else:
                    est = extract_pending_date(orig)
                    est_txt = f" → 예상 {est}" if est else ""
                    st.markdown(f'<span class="date-pend">{disp}{est_txt}</span>',
                                unsafe_allow_html=True)

            with c3:
                if st.button("🔍 교차검증", key=f"dart_{company}"):
                    with st.spinner(f"{company} 조회 중…"):
                        found, status, detail = search_agm_date(company, dart_api_key, anthropic_key)
                        crawl_results[company] = {
                            "date": found, "source": status, "detail": detail}
                        st.session_state["crawl_results"] = crawl_results
                        if found and found != disp:
                            state["overrides"][company] = found
                            state.setdefault("updated_recently", set()).add(company)
                            state.setdefault("updated_timestamps", {})[company] = datetime.now().isoformat()
                            save_state(state)
                            st.rerun()

                if company in crawl_results:
                    res = crawl_results[company]
                    detail = res.get("detail", {})
                    dart_d,  dart_s  = detail.get("dart",  (None, ""))
                    kvote_d, kvote_s = detail.get("kvote", (None, ""))

                    if res["date"] and res["date"] != disp:
                        st.markdown(f'<span class="dart-ok">→ {res["date"]}</span>',
                                    unsafe_allow_html=True)
                    elif res["date"]:
                        st.markdown('<span class="dart-same">✓ 동일</span>',
                                    unsafe_allow_html=True)

                    # 소스별 결과 미니 표시
                    dart_icon  = "✅" if validate_march_2026(dart_d)  else "✗"
                    kvote_icon = "✅" if validate_march_2026(kvote_d) else "✗"
                    st.caption(f"DART {dart_icon} {dart_d or dart_s[:15]}")
                    st.caption(f"K-Vote {kvote_icon} {kvote_d or kvote_s[:15]}")
                    if "⚠️" in res["source"]:
                        st.markdown(f'<span class="dart-err">{res["source"]}</span>',
                                    unsafe_allow_html=True)

            with c4:
                inline_active = st.session_state.get("inline_change") == company
                btn_label = "✖ 취소" if inline_active else "✏️ 기업변경"
                if st.button(btn_label, key=f"chg_{company}"):
                    st.session_state["inline_change"] = None if inline_active else company
                    st.rerun()

            # ── 이전 기업 정보 펼침 ──
            if has_prev and company in st.session_state["expanded_prev"]:
                prev = changes[company]
                st.markdown(
                    f'<div style="background:#f1f5f9;border-left:4px solid #94a3b8;'
                    f'border-radius:0 6px 6px 0;padding:7px 14px;margin:3px 0 4px 0;'
                    f'font-size:.85em;color:#475569;">'
                    f'🔁 <strong>변경 1회 전</strong>: {prev["prev_name"]} '
                    f'| 날짜: {prev["prev_date"]} '
                    f'| {prev["changed_at"]}</div>',
                    unsafe_allow_html=True)

            # ── 인라인 기업변경 폼 ──
            if st.session_state.get("inline_change") == company:
                with st.container():
                    st.markdown(
                        f'<div style="background:#fffbeb;border:1.5px solid #f59e0b;'
                        f'border-radius:8px;padding:14px 18px;margin:6px 0 10px 0;">',
                        unsafe_allow_html=True)
                    st.markdown(
                        f"✏️ **{company}** 을 다른 기업으로 교체합니다. "
                        f"기존 기업은 '변경 1회 전'으로 기록됩니다.")
                    ic1, ic2 = st.columns([3, 2])
                    with ic1:
                        new_name = st.text_input(
                            "새 기업명", placeholder="예: 삼성SDI",
                            key=f"ic_name_{company}")
                    with ic2:
                        opt = st.radio(
                            "날짜", ["직접 입력", "미정"],
                            horizontal=True, key=f"ic_opt_{company}")
                    new_date = "미정"
                    if opt == "직접 입력":
                        new_date = st.date_input(
                            "날짜 선택", key=f"ic_date_{company}"
                        ).strftime("%Y-%m-%d")
                    bc1, bc2 = st.columns([1, 1])
                    with bc1:
                        if st.button("✅ 확정", type="primary",
                                     use_container_width=True,
                                     key=f"ic_ok_{company}"):
                            nn = (new_name or "").strip()
                            if nn:
                                state.setdefault("changes", {})[nn] = {
                                    "prev_name": company,
                                    "prev_date": state["overrides"].get(company, orig),
                                    "changed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                }
                                state["overrides"].pop(company, None)
                                state["overrides"][nn] = new_date
                                state.setdefault("name_replacements", {})[company] = nn
                                save_state(state)
                                load_excel_data.clear()
                                st.session_state["inline_change"] = None
                                st.rerun()
                            else:
                                st.error("기업명을 입력하세요.")
                    with bc2:
                        if st.button("❌ 취소", use_container_width=True,
                                     key=f"ic_cancel_{company}"):
                            st.session_state["inline_change"] = None
                            st.rerun()
                    st.markdown("</div>", unsafe_allow_html=True)

        st.markdown("")


# ─────────────────────────────────────────────
# 기업 변경 모달
# ─────────────────────────────────────────────

def render_change_modal(state: dict):
    info = st.session_state.get("change_modal")
    if not info:
        return

    old_name  = info["old_name"]
    orig_date = info["orig_date"]

    with st.expander(f"✏️ 기업 변경: {old_name}", expanded=True):
        st.info(f"**{old_name}** 을 다른 기업으로 교체합니다.\n\n기존 기업은 '변경 1회 전 기업'으로 기록됩니다.")
        new_name = st.text_input("새 기업명", placeholder="예: 삼성SDI", key="nci")
        opt = st.radio("새 기업 주주총회 날짜", ["직접 입력", "미정"], horizontal=True, key="ndo")
        new_date = "미정"
        if opt == "직접 입력":
            new_date = st.date_input("날짜 선택", key="ndi").strftime("%Y-%m-%d")

        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ 확정", type="primary", use_container_width=True):
                if new_name.strip():
                    nn = new_name.strip()
                    state.setdefault("changes", {})[nn] = {
                        "prev_name": old_name,
                        "prev_date": state["overrides"].get(old_name, orig_date),
                        "changed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    }
                    state["overrides"].pop(old_name, None)
                    state["overrides"][nn] = new_date
                    state.setdefault("name_replacements", {})[old_name] = nn
                    save_state(state)
                    load_excel_data.clear()
                    st.session_state["change_modal"] = None
                    st.rerun()
                else:
                    st.error("기업명을 입력하세요.")
        with c2:
            if st.button("❌ 취소", use_container_width=True):
                st.session_state["change_modal"] = None
                st.rerun()


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────

def render_search_log():
    """검색 완료 후 업데이트/미변경 결과를 메인 화면에 표시"""
    log = st.session_state.get("search_log")
    if not log:
        return

    updated   = log.get("updated", [])
    unchanged = log.get("unchanged", [])
    no_result = log.get("no_result", [])
    ran_at    = log.get("ran_at", "")
    lbl_map   = {"full": "전체 교차검증", "kvote": "K-Vote", "ai": "AI 웹검색"}
    lbl       = lbl_map.get(log.get("label", ""), "검색")

    with st.expander(
        f"🔎 [{lbl}] 검색 결과 — {ran_at}  "
        f"| 업데이트 **{len(updated)}건** / 변경없음 {len(unchanged)}건 / 미조회 {len(no_result)}건",
        expanded=True,
    ):
        col_close, _ = st.columns([1, 5])
        with col_close:
            if st.button("✖ 닫기", key="close_log"):
                st.session_state["search_log"] = None
                st.rerun()

        if updated:
            st.markdown("#### 📢 업데이트된 기업")
            rows = []
            for corp, old, new, src in updated:
                rows.append({
                    "기업": corp,
                    "이전 날짜": old or "미정",
                    "새 날짜": new,
                    "출처": src,
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.info("업데이트된 기업이 없습니다.")

        if unchanged:
            with st.expander(f"변경 없음 ({len(unchanged)}건)"):
                rows2 = [{"기업": c, "날짜": d, "출처": s} for c, d, s in unchanged]
                st.dataframe(pd.DataFrame(rows2), use_container_width=True, hide_index=True)

        if no_result:
            with st.expander(f"조회 실패 / 공시 없음 ({len(no_result)}건)"):
                rows3 = [{"기업": c, "사유": s} for c, s in no_result]
                st.dataframe(pd.DataFrame(rows3), use_container_width=True, hide_index=True)


def render_ai_results_tab(df: pd.DataFrame, state: dict):
    """AI 웹검색 결과 탭 — 전체 종목 리스트"""
    crawl = st.session_state.get("crawl_results", {})
    overrides = state["overrides"]

    st.markdown("### 🤖 AI 웹검색 주총일 확인 결과")
    st.caption("사이드바의 '🤖 AI 웹검색만' 또는 '🔍 전체 교차검증' 실행 후 결과가 표시됩니다.")

    rows = []
    for _, row in df.iterrows():
        corp   = row["단체명"]
        cur    = overrides.get(corp, row["주주총회일"])
        res    = crawl.get(corp, {})
        detail = res.get("detail", {})
        ai_d, ai_s = detail.get("ai", (None, "미검색"))
        dart_d, _  = detail.get("dart",  (None, ""))
        kvote_d, _ = detail.get("kvote", (None, ""))

        match = ""
        if validate_march_2026(ai_d):
            if ai_d == dart_d == kvote_d:
                match = "✅ 3소스 일치"
            elif ai_d == dart_d or ai_d == kvote_d:
                match = "🟡 2소스 일치"
            elif validate_march_2026(dart_d) or validate_march_2026(kvote_d):
                match = "⚠️ 불일치"
            else:
                match = "🔵 AI 단독"

        rows.append({
            "기업":      corp,
            "현재 날짜": cur,
            "AI 검색 결과": ai_d or "—",
            "DART":     dart_d or "—",
            "K-Vote":   kvote_d or "—",
            "교차검증":  match or ("—" if not ai_d else "미검색"),
            "AI 소스":   (ai_s or "")[:60],
            "필수단체":  "🔴" if row.get("비고") == "필수단체" else "",
        })

    result_df = pd.DataFrame(rows)

    # 필터
    fcol1, fcol2, fcol3 = st.columns(3)
    with fcol1:
        show_filter = st.selectbox(
            "필터", ["전체", "AI결과 있음", "업데이트 가능", "교차확인됨", "미검색"],
            key="ai_tab_filter")
    with fcol2:
        req_only = st.checkbox("필수단체만", key="ai_tab_req")
    with fcol3:
        search_q = st.text_input("기업명 검색", key="ai_tab_search", placeholder="검색…")

    fdf = result_df.copy()
    if show_filter == "AI결과 있음":
        fdf = fdf[fdf["AI 검색 결과"] != "—"]
    elif show_filter == "업데이트 가능":
        fdf = fdf[(fdf["AI 검색 결과"] != "—") & (fdf["AI 검색 결과"] != fdf["현재 날짜"])]
    elif show_filter == "교차확인됨":
        fdf = fdf[fdf["교차검증"].str.contains("일치", na=False)]
    elif show_filter == "미검색":
        fdf = fdf[fdf["AI 검색 결과"] == "—"]
    if req_only:
        fdf = fdf[fdf["필수단체"] == "🔴"]
    if search_q:
        fdf = fdf[fdf["기업"].str.contains(search_q, na=False)]

    st.caption(f"표시: {len(fdf)} / 전체 {len(result_df)}건")

    st.dataframe(
        fdf.drop(columns=["필수단체"]),
        use_container_width=True,
        hide_index=True,
        column_config={
            "기업":          st.column_config.TextColumn(width="medium"),
            "AI 검색 결과":  st.column_config.TextColumn(width="small"),
            "현재 날짜":     st.column_config.TextColumn(width="small"),
            "DART":          st.column_config.TextColumn(width="small"),
            "K-Vote":        st.column_config.TextColumn(width="small"),
            "교차검증":      st.column_config.TextColumn(width="medium"),
        }
    )

    # 일괄 적용 버튼
    applicable = fdf[
        (fdf["AI 검색 결과"] != "—") &
        (fdf["AI 검색 결과"] != fdf["현재 날짜"])
    ]
    if not applicable.empty:
        st.markdown(f"---")
        st.markdown(f"**📢 적용 가능한 업데이트: {len(applicable)}건**")
        if st.button(f"✅ AI 검색 결과 {len(applicable)}건 일괄 적용",
                     type="primary", key="ai_apply_all"):
            for _, r in applicable.iterrows():
                corp = r["기업"]
                new_d = r["AI 검색 결과"]
                if validate_march_2026(new_d):
                    state["overrides"][corp] = new_d
                    state.setdefault("updated_recently", set()).add(corp)
                    state.setdefault("updated_timestamps", {})[corp] = datetime.now().isoformat()
            save_state(state)
            st.success(f"{len(applicable)}건 적용 완료!")
            st.rerun()


def main():
    init_session()
    state = st.session_state["state"]
    dart_api_key, anthropic_key = render_sidebar(state)

    # 헤더
    st.title("📅 주주총회 일정 트래커")
    ts = max(state.get("updated_timestamps", {}).values(), default=None)
    if ts:
        st.caption(f"마지막 업데이트: {ts[:16]}")

    # 검색 결과 로그 (있을 때만 표시)
    render_search_log()

    # 데이터 로드
    try:
        df = load_excel_data()
    except FileNotFoundError:
        st.error(f"'{EXCEL_PATH}' 파일을 app.py 와 같은 폴더에 넣어주세요.")
        return

    # 기업명 교체
    repl = state.get("name_replacements", {})
    if repl:
        df["단체명"] = df["단체명"].replace(repl)
        df = df.drop_duplicates(subset=["단체명"]).reset_index(drop=True)

    # ── 탭 ──
    tab_cal, tab_list, tab_ai = st.tabs(["📅 달력", "📋 리스트", "🤖 AI 검색결과"])

    with tab_cal:
        overrides = state["overrides"]
        conf_n = sum(1 for _, r in df.iterrows()
                     if is_confirmed(overrides.get(r["단체명"], r["주주총회일"])))
        pend_n = len(df) - conf_n
        upd_n  = len(state.get("updated_recently", set()))

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("전체 기업", len(df))
        m2.metric("📅 확정", conf_n, delta=f"+{upd_n} 업데이트" if upd_n else None)
        m3.metric("⏳ 미정", pend_n)
        m4.metric("🔴 필수단체", int((df["비고"] == "필수단체").sum()))
        st.markdown("---")
        st.subheader("2026년 3월")
        day_map = build_day_map(df, state)
        st.markdown(render_calendar_html(2026, 3, day_map), unsafe_allow_html=True)

    with tab_list:
        render_list_view(df, state, dart_api_key, anthropic_key)

    with tab_ai:
        render_ai_results_tab(df, state)


if __name__ == "__main__":
    main()
