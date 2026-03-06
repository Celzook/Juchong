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
        ("state",         load_state()),
        ("change_modal",  None),
        ("expanded_prev", set()),
        ("crawl_results", {}),
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
    """강화된 정규식 + '일시' 키워드 우선으로 주주총회 확정일 추출"""
    try:
        resp = requests.get(
            "https://opendart.fss.or.kr/api/document.xml",
            params={"crtfc_key": api_key, "rcept_no": rcept_no.replace("-", "")},
            timeout=25
        )
        resp.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        xml_names = [f for f in zf.namelist() if f.lower().endswith(".xml")]
        if not xml_names:
            return None
        raw = zf.read(xml_names[0]).decode("utf-8", errors="ignore")

        # 디버깅용 (필요시 주석 해제)
        # print(raw[:4000])  # 처음 4000자 출력해서 어디서 날짜 나오는지 확인

        # 0순위: 가장 흔한 패턴 "일시 : 2026년 3월 24일" (셀트리온 등)
        p0 = re.compile(
            r"(?:일시|개최일시|총회일시|주주총회\s*일시|소집일시)[:\s]*"
            r"(\d{4})[년.\s\-]*(\d{1,2})[월.\s\-]*(\d{1,2})",
            re.IGNORECASE
        )

        # 당신 기존 패턴들 (이미 좋음)
        p1 = re.compile(
            r"(?:주주총회|정기총회|소집일|개최일|총회일)[^0-9]{0,30}?"
            r"(\d{4})[년.\s\-]*(\d{1,2})[월.\s\-]*(\d{1,2})",
            re.IGNORECASE
        )
        p2 = re.compile(
            r"(\d{4})[.\-년\s]+(\d{1,2})[.\-월\s]+(\d{1,2})"
            r"[^0-9]{0,30}(?:주주총회|정기총회|소집|개최)",
            re.IGNORECASE
        )
        p3 = re.compile(r"\b(2026)[.\-](0?[1-9]|1[0-2])[.\-](0?[1-9]|[12]\d|3[01])\b")

        for pat in [p0, p1, p2, p3]:  # p0을 맨 앞에 → '일시' 우선
            for m in pat.finditer(raw):
                y, mo, d = m.group(1), m.group(2), m.group(3)
                date_str = f"{y}-{int(mo):02d}-{int(d):02d}"
                if validate_march_2026(date_str):
                    return date_str

        # 최후 수단 (당신 거 유지)
        m = re.search(r"2026[.\s\-년]*0?3[.\s\-월]*([0-2]?\d|3[01])[.\s일]*", raw)
        if m:
            candidate = f"2026-03-{int(m.group(1)):02d}"
            if validate_march_2026(candidate):
                return candidate

    except Exception as e:
        print(f"parse_agm_date 오류: {e}")  # 디버깅용
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

        # 1순위: 소집결의 (정정 포함)
    for item in items:
        report_nm = item.get("report_nm", "")
        if "주주총회소집결의" in report_nm or "[기재정정]주주총회소집결의" in report_nm:
            rcept_no = item["rcept_no"]
            report_nm_full = report_nm
            rcept_dt = item.get("rcept_dt", "")  # 참고용
            break

    # 2순위: 소집공고
    if not rcept_no:
        for item in items:
            report_nm = item.get("report_nm", "")
            if "주주총회소집공고" in report_nm:
                rcept_no = item["rcept_no"]
                report_nm_full = report_nm
                rcept_dt = item.get("rcept_dt", "")
                break

    # 3순위: 주주총회 관련 모든 공시
    if not rcept_no:
        for item in items:
            nm = item.get("report_nm", "")
            if any(kw in nm for kw in ["주주총회", "정기총회", "소집결의", "소집공고", "총회소집"]):
                rcept_no = item["rcept_no"]
                report_nm_full = nm
                rcept_dt = item.get("rcept_dt", "")
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
# ② K-Vote (한국예탁결제원 전자투표) 크롤링
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# ② K-Vote (한국예탁결제원 전자투표) 크롤링
# ─────────────────────────────────────────────
def search_kvote(company_name: str) -> tuple[str | None, str]:
    """ evote.ksd.or.kr 주주총회 일정 검색 POST /evote/main/agm/agmScheduleList.do """
    
    # ← 여기! 함수 맨 위(try 바로 위 또는 바로 아래)에 추가
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://evote.ksd.or.kr/",
        "Connection": "keep-alive"
    }
    
    try:
        url = "https://evote.ksd.or.kr/evote/main/agm/agmScheduleList.do"
        payload = {
            "agmSchdSrchCnt": "50",
            "agmSchdSrchTypCd": "1",  # 1=회사명
            "srchCrpNm": company_name,
            "agmSchdSrchYr": "2026",
        }
        resp = requests.post(url, data=payload, headers=HEADERS, timeout=12)
        resp.raise_for_status()
        
        # ... (나머지 코드 그대로: BeautifulSoup 파싱, JSON 대비 등)
        
    except requests.exceptions.ConnectionError:
        return None, "K-Vote: 네트워크 오류"
    except Exception as e:
        return None, f"K-Vote 오류: {e}"


# ─────────────────────────────────────────────
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

def render_sidebar(state: dict) -> str:
    st.sidebar.title("⚙️ 설정")

    dart_api_key = st.sidebar.text_input(
        "DART OpenAPI 키",
        type="password",
        help="opendart.fss.or.kr 무료 발급",
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
    else:
        st.sidebar.info("API 키 입력 후 DART 검색 가능")

    st.sidebar.markdown("---")

    if st.sidebar.button("🔍 전체 교차검증 검색", use_container_width=True):
        df = load_excel_data()
        if dart_api_key:
            try:
                with st.spinner("기업코드 로딩…"):
                    load_corp_codes(dart_api_key)
            except Exception as e:
                st.sidebar.error(str(e))
                return dart_api_key

        prog  = st.sidebar.progress(0)
        results = {}
        corps = df["단체명"].tolist()
        for i, corp in enumerate(corps):
            found, status, detail = search_agm_date(corp, dart_api_key)
            results[corp] = {"date": found, "source": status, "detail": detail}
            prog.progress((i + 1) / len(corps), text=f"{corp}…")
            time.sleep(0.5)
        prog.empty()
        st.session_state["crawl_results"] = results

        updated_n = 0
        for corp, info in results.items():
            if info["date"]:
                r = df[df["단체명"] == corp]
                if not r.empty:
                    cur = state["overrides"].get(corp, r.iloc[0]["주주총회일"])
                    if info["date"] != cur:
                        state["overrides"][corp] = info["date"]
                        state.setdefault("updated_recently", set()).add(corp)
                        state.setdefault("updated_timestamps", {})[corp] = datetime.now().isoformat()
                        updated_n += 1
        save_state(state)
        msg = f"✅ {updated_n}개 업데이트됨" if updated_n else "변경 없음"
        st.sidebar.success(msg)
        st.rerun()

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
**달력 범례**

🟢 초록 칩 = 확정  
🔵 파란 칩 = 확정 (필수단체)  
🟡 노란 칩 = DART 업데이트됨  
🟠 점선 칩 = 미정 (작년 날짜 기준)  
★ = 필수단체  
* = 미정 표시
""")

    return dart_api_key


# ─────────────────────────────────────────────
# 리스트 뷰
# ─────────────────────────────────────────────

def render_list_view(df: pd.DataFrame, state: dict, dart_api_key: str):
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
                        found, status, detail = search_agm_date(company, dart_api_key)
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
                if st.button("✏️ 기업변경", key=f"chg_{company}"):
                    st.session_state["change_modal"] = {
                        "old_name": company, "orig_date": orig}
                    st.rerun()

            if has_prev and company in st.session_state["expanded_prev"]:
                prev = changes[company]
                st.markdown(
                    f'<div style="background:#f1f5f9;border-left:4px solid #94a3b8;'
                    f'border-radius:0 6px 6px 0;padding:7px 14px;margin:3px 0 8px 0;'
                    f'font-size:.85em;color:#475569;">'
                    f'🔁 <strong>변경 1회 전</strong>: {prev["prev_name"]} '
                    f'| 날짜: {prev["prev_date"]} '
                    f'| {prev["changed_at"]}</div>',
                    unsafe_allow_html=True)

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

def main():
    init_session()
    state = st.session_state["state"]
    dart_api_key = render_sidebar(state)

    # 헤더
    col_t, col_v = st.columns([5, 2])
    with col_t:
        st.title("📅 주주총회 일정 트래커")
        ts = max(state.get("updated_timestamps", {}).values(), default=None)
        if ts:
            st.caption(f"마지막 DART 업데이트: {ts[:16]}")
    with col_v:
        st.markdown("<br>", unsafe_allow_html=True)
        view = st.radio("보기 모드", ["📅 달력", "📋 리스트"],
                        horizontal=True, key="view_radio")

    render_change_modal(state)

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

    if "달력" in view:
        # 통계
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

        day_map = build_day_map(df, state)

        # 3월 달력 고정
        st.subheader("2026년 3월")
        st.markdown(render_calendar_html(2026, 3, day_map),
                    unsafe_allow_html=True)

    else:
        render_list_view(df, state, dart_api_key)


if __name__ == "__main__":
    main()
