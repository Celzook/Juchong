import streamlit as st
import pandas as pd
import json, os, re, time, calendar, zipfile, io, html
import xml.etree.ElementTree as ET
import requests
from datetime import datetime, date
from bs4 import BeautifulSoup
try:
    import OpenDartReader
    HAS_ODR = True
except ImportError:
    HAS_ODR = False

try:
    from github import Github, GithubException
    HAS_GITHUB = True
except ImportError:
    HAS_GITHUB = False

st.set_page_config(page_title="주주총회 일정 트래커", page_icon="📅",
                   layout="wide", initial_sidebar_state="expanded")

EXCEL_PATH      = "주주총회.xlsx"
STATE_PATH      = "agm_state.json"
CORP_CACHE      = "dart_corp_codes.json"
GH_STATE_PATH   = "agm_state.json"   # GitHub 상에서의 state 파일 경로

# ── CSS ──────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;600;700&display=swap');
* { font-family:'Noto Sans KR',sans-serif; box-sizing:border-box; }
@keyframes pulse-gold{0%,100%{background:#ffe066}50%{background:#fff3a0}}
.updated-badge{display:inline-block;background:#ffe066;color:#7a5800;font-weight:700;
  font-size:.75em;padding:1px 7px;border-radius:10px;margin-left:5px;
  border:1px solid #f5c518;animation:pulse-gold 1.1s ease-in-out 5;}
.cal-wrap{overflow-x:auto;}
table.cal{width:100%;border-collapse:collapse;table-layout:fixed;}
table.cal th{background:#1e3a5f;color:#fff;text-align:center;padding:8px 4px;font-size:.82em;font-weight:600;}
table.cal th.week-col{background:#0f2540;font-size:.78em;}
table.cal td{vertical-align:top;border:1px solid #dde3ed;padding:5px 5px 8px;background:#fff;font-size:.8em;}
table.cal td.week-total{background:#f0f4fa;text-align:center;vertical-align:middle;font-weight:700;color:#1e3a5f;font-size:.85em;border:1px solid #c5d0e0;}
table.cal td.empty{background:#f8f9fc;}
table.cal td.today{background:#fffbe6;border:2px solid #f5c518;}
.cal-day-num{font-weight:700;font-size:.9em;color:#374151;margin-bottom:4px;display:flex;align-items:center;gap:4px;}
.day-badge{background:#1e3a5f;color:#fff;font-size:.7em;font-weight:700;border-radius:8px;padding:1px 6px;min-width:22px;text-align:center;}
.day-badge.has-pending{background:#b45309;}
.chip{display:inline-block;border-radius:10px;padding:2px 7px;margin:2px 2px 0 0;font-size:.73em;font-weight:500;line-height:1.6;cursor:help;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.chip-confirmed{background:#dcfce7;color:#166534;border:1px solid #86efac;}
.chip-confirmed.req{background:#dbeafe;color:#1e40af;border:1px solid #93c5fd;}
.chip-updated{background:#fef9c3;color:#854d0e;border:1.5px solid #fde047;}
.chip-pending{background:#fff7ed;color:#9a3412;border:1px dashed #fdba74;font-style:italic;}
.chip-pending.req{background:#fef3c7;color:#92400e;border:1px dashed #fcd34d;}
.chip-done{background:#fce7f3;color:#9d174d;border:1.5px solid #f9a8d4;}
.chip-done.req{background:#fdf2f8;color:#831843;border:1.5px solid #f472b6;}
.week-cnt{font-size:1.15em;} .week-sub{font-size:.68em;color:#64748b;margin-top:3px;}
.date-conf{color:#166534;font-weight:600;} .date-pend{color:#9a3412;font-style:italic;}
.src-ok{background:#dcfce7;color:#166534;font-size:.78em;padding:2px 8px;border-radius:8px;font-weight:600;}
.src-same{color:#6b7280;font-size:.78em;} .src-err{color:#dc2626;font-size:.78em;}
.manual-box{background:#f0fdf4;border:1.5px solid #86efac;border-radius:8px;padding:14px 18px;margin:6px 0 10px;}
.change-box{background:#fffbeb;border:1.5px solid #f59e0b;border-radius:8px;padding:14px 18px;margin:6px 0 10px;}
</style>
""", unsafe_allow_html=True)


# ── 데이터 / 상태 ─────────────────────────────

@st.cache_data
def load_excel_data() -> pd.DataFrame:
    df = pd.read_excel(EXCEL_PATH, sheet_name="리스트 대상 운용사", usecols="B:E", header=1)
    df.columns = ["단체명", "주주총회일", "비고", "운용사"]
    df = df.dropna(subset=["단체명"])
    df["단체명"]    = df["단체명"].astype(str).str.strip()
    df["주주총회일"] = df["주주총회일"].apply(
        lambda x: x.strftime("%Y-%m-%d") if hasattr(x,"strftime") else str(x).strip())
    df["비고"]   = df["비고"].fillna("").astype(str).str.strip()
    df["운용사"] = df["운용사"].fillna("").astype(str).str.strip()
    return df.reset_index(drop=True)


def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        try:
            raw = json.load(open(STATE_PATH, encoding="utf-8"))
            raw["updated_recently"] = set(raw.get("updated_recently", []))
            # change_history 없으면 changes 에서 마이그레이션
            if "change_history" not in raw:
                ch = {}
                for k, v in raw.get("changes", {}).items():
                    ch[k] = v if isinstance(v, list) else [v]
                raw["change_history"] = ch
            return raw
        except Exception:
            pass
    return {"overrides":{}, "changes":{}, "change_history":{},
            "updated_recently":set(), "updated_timestamps":{},
            "name_replacements":{}, "agenda_status":{}, "done_status":{}}


def save_state(state: dict):
    out = dict(state)
    out["updated_recently"] = list(state.get("updated_recently", set()))
    json.dump(out, open(STATE_PATH,"w",encoding="utf-8"), ensure_ascii=False, indent=2)


def init_session():
    for k, v in [
        ("state",               load_state()),
        ("inline_change",       None),
        ("inline_manual",       None),
        ("crawl_results",       {}),
        ("pending_updates",     {}),
        ("apply_selected",      set()),
        ("change_history_open", set()),
        ("agenda_open",         set()),   # 의안 드롭다운 열린 기업
        ("agenda_cache_mem",    {}),      # 메모리 캐시 {company: [agendas]}
        ("search_log",          None),
    ]:
        if k not in st.session_state:
            st.session_state[k] = v


# ── 날짜 유틸 ─────────────────────────────────

def is_confirmed(s) -> bool:
    return bool(re.match(r"\d{4}-\d{2}-\d{2}", str(s)))

def validate_march_2026(d) -> bool:
    return bool(d and re.match(r"2026-03-\d{2}$", str(d)))

def extract_pending_date(s: str, year: int = 2026):
    m = re.search(r"(\d{1,2})\.(\d{1,2})", str(s))
    return f"{year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else None


# ── DART OpenAPI ──────────────────────────────

def load_corp_codes(api_key: str) -> dict:
    if os.path.exists(CORP_CACHE) and time.time()-os.path.getmtime(CORP_CACHE) < 86400:
        return json.load(open(CORP_CACHE, encoding="utf-8"))
    r = requests.get(
        f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={api_key}", timeout=30)
    r.raise_for_status()
    root = ET.fromstring(zipfile.ZipFile(io.BytesIO(r.content)).read("CORPCODE.xml"))
    d = {item.findtext("corp_name","").strip(): item.findtext("corp_code","").strip()
         for item in root.findall("list") if item.findtext("stock_code","").strip()}
    json.dump(d, open(CORP_CACHE,"w",encoding="utf-8"), ensure_ascii=False)
    return d


def find_corp_code(corp_dict: dict, name: str):
    if name in corp_dict: return corp_dict[name]
    low = name.lower()
    for k,v in corp_dict.items():
        if k.lower() == low: return v
    cands = sorted([(k,v) for k,v in corp_dict.items() if name in k or k in name],
                   key=lambda x: abs(len(x[0])-len(name)))
    return cands[0][1] if cands else None


def _parse_dart_date(text: str):
    """BeautifulSoup get_text() 결과에서 주총 일시 추출 → 'YYYY-MM-DD' 또는 None"""
    # 패턴 1: "일 시 ... 2026년 3월 26일" (사용자 코드와 동일)
    m = re.search(r'일\s*시.*?(\d{4})[-년]\s*(\d{1,2})[-월]\s*(\d{1,2})[일]?', text, re.DOTALL)
    if m:
        d = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        if validate_march_2026(d):
            return d
    # 패턴 2: "2026년 3월 26일" 형태 전체 문서에서
    m = re.search(r'(2026)[년\s.]*(\d{1,2})[월\s.]*(\d{1,2})[일]', text)
    if m:
        d = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        if validate_march_2026(d):
            return d
    # 패턴 3: 숫자 형식 2026-03-XX / 2026.03.XX
    m = re.search(r'\b(2026)[.\-](0?[1-9]|1[0-2])[.\-](0?[1-9]|[12]\d|3[01])\b', text)
    if m:
        d = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        if validate_march_2026(d):
            return d
    return None


def search_dart_api(company_name: str, api_key: str):
    """OpenDartReader로 주총 확정일 조회. Returns (날짜|None, 메시지)"""
    if not api_key:
        return None, "API 키 없음"
    if not HAS_ODR:
        return None, "OpenDartReader 미설치 (pip install opendartreader)"
    try:
        # 기업코드 조회 (corp_code.xml 캐시 활용)
        corp_dict = load_corp_codes(api_key)
        corp_code = find_corp_code(corp_dict, company_name)
        if not corp_code:
            return None, "기업코드 미발견"

        dart = OpenDartReader(api_key)
        year = datetime.now().year
        # kind 지정 없이 전체 공시 조회 — 주주총회소집결의는 A(정기) 아닌 경우도 있음
        reports = dart.list(corp_code, start=f"{year}-01-01", end=f"{year}-03-31")

        if not isinstance(reports, pd.DataFrame) or reports.empty:
            return None, "공시 없음"

        # 소집결의 → 소집공고 순으로 우선
        agm = reports[reports["report_nm"].str.contains("주주총회소집", na=False)]
        if agm.empty:
            return None, "주주총회소집 공시 없음"

        latest      = agm.iloc[0]
        rcept_no    = latest["rcept_no"]
        report_nm   = latest["report_nm"]

        # 공시 원문 HTML → BeautifulSoup → 텍스트
        doc_html = dart.document(rcept_no)
        soup     = BeautifulSoup(doc_html, "html.parser")
        text     = soup.get_text()

        found = _parse_dart_date(text)
        if found:
            return found, f"DART ({report_nm[:20]})"

        return None, f"날짜 파싱 실패 ({report_nm[:20]})"

    except requests.exceptions.ConnectionError:
        return None, "네트워크 오류"
    except Exception as e:
        return None, f"오류: {str(e)[:50]}"


# ── DART 의안 조회 ────────────────────────────

AGENDA_CACHE_PATH = "dart_agenda_cache.json"


def _load_agenda_cache() -> dict:
    if os.path.exists(AGENDA_CACHE_PATH):
        try:
            return json.load(open(AGENDA_CACHE_PATH, encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_agenda_cache(cache: dict):
    json.dump(cache, open(AGENDA_CACHE_PATH, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def _parse_agendas(text: str) -> list:
    """
    의결권대리행사권유참고서류/소집공고에서 의안 계층 추출.
    Returns: [{"no","title","type","is_sub","parent_no","candidate"}]
      - N호  : 부모 의안
      - N-M호: 자식 의안 (parent_no = N호)
    """
    items = []
    seen  = set()
    SPECIAL_KW = ['정관','합병','분할','해산','자본감소','영업양도']

    def normalize(no_raw: str) -> str:
        return f"제{no_raw.replace('－','-')}호"

    def extract_candidate(title: str) -> str:
        """제목에서 후보자 이름 추출 (한국인 이름 2~4자)"""
        # "사내이사 홍길동 선임" 형태
        m = re.search(r'(?:사내|사외|기타비상무|비상무)\s*이사\s+([가-힣]{2,4})\s+선임', title)
        if m: return m.group(1)
        # "- 홍길동 (" 형태
        m = re.search(r'[-–]\s*([가-힣]{2,4})\s*[\(\s]', title)
        if m: return m.group(1)
        # "홍길동 선임의 건" — 이름이 제목 앞에
        m = re.search(r'^([가-힣]{2,4})\s+선임', title)
        if m: return m.group(1)
        return ""

    def add(no_raw: str, title_raw: str):
        no    = normalize(no_raw)
        title = re.sub(r'\s+', ' ', title_raw).strip().strip(':：- ').strip()
        title = re.sub(r'\s*[\(\（](보통결의|특별결의)[\)\）]', '', title).strip()
        if no in seen or len(title) < 4:
            return
        is_sub    = '-' in no_raw.replace('－','-')
        parent_no = normalize(no_raw.split('-')[0]) if is_sub else None
        candidate = extract_candidate(title)
        items.append({
            "no": no, "title": title, "type": "",
            "is_sub": is_sub, "parent_no": parent_no,
            "candidate": candidate,
        })
        seen.add(no)

    # P1: 한글 "제N(-M)호 의안" (공백·탭·콜론·대시 자유)
    matches = list(re.finditer(
        r'제\s*(\d+(?:[-－]\d+)?)\s*호\s*의\s*안\s*[:\-：\t ]\s*([^\n\r]{2,120})',
        text, re.MULTILINE))
    if not matches:
        # P2: 한자 "第N號 議案"
        matches = list(re.finditer(
            r'第\s*(\d+)\s*號\s*議案\s*[:\-：\s]\s*([^\n\r]{2,80})', text, re.MULTILINE))
    if not matches:
        # P3: "N. XXX의 건" 번호형
        matches = list(re.finditer(
            r'^\s*(\d{1,2})\.\s+([가-힣][^\n\r]{2,70}(?:의\s*건|승인|선임|변경|개정|결정))',
            text, re.MULTILINE))
    if not matches:
        # P4: "□ 안건N :" 형태
        matches = list(re.finditer(
            r'[□■◆]\s*안건\s*(\d+)\s*[:：]\s*([^\n\r]{4,80})', text, re.MULTILINE))
    if not matches:
        # P5: "의안 N." 형태
        matches = list(re.finditer(
            r'의안\s*(\d+)[.\s：:]\s*([^\n\r]{4,80})', text, re.MULTILINE))
    if not matches:
        # P6: "안건 제N호:" 형태
        matches = list(re.finditer(
            r'안건\s*제\s*(\d+)\s*호\s*[:：]\s*([^\n\r]{4,80})', text, re.MULTILINE))

    for m in matches:
        add(m.group(1).replace('－','-'), m.group(2))

    # ── 결의유형 감지 ──
    for item in items:
        t       = item['title']
        pos     = text.find(item['no'])
        snippet = text[max(0, pos-50):pos+400] if pos >= 0 else ''
        if '특별결의' in t or '특별결의' in snippet:
            item['type'] = '특별결의'
        elif '보통결의' in t or '보통결의' in snippet:
            item['type'] = '보통결의'
        elif any(k in t for k in SPECIAL_KW):
            item['type'] = '특별결의(추정)'
        else:
            item['type'] = '보통결의(추정)'
        item['title'] = re.sub(
            r'\s*[\(\（](보통결의|특별결의)[추정()（）]*[\)\）]?', '', item['title']).strip()

    return items


def _group_agendas(items: list) -> list:
    """
    items를 계층 구조로 정렬.
    부모 N호 뒤에 자식 N-M호가 오도록 재배치.
    부모 없는 N-M호(예: SK하이닉스)는 가상 그룹으로 묶음.
    """
    parents   = [it for it in items if not it['is_sub']]
    subs      = [it for it in items if it['is_sub']]
    parent_nos = {p['no'] for p in parents}
    sub_map   = {}
    for s in subs:
        sub_map.setdefault(s['parent_no'], []).append(s)

    result = []
    for p in parents:
        result.append(p)
        result.extend(sub_map.get(p['no'], []))

    # 부모 없는 서브의안 처리 (N-M호만 있는 경우)
    for pno, children in sub_map.items():
        if pno not in parent_nos:
            # 가상 부모 삽입
            virtual_title = children[0]['title'].split(' 선임')[0] + ' 선임의 건' \
                if '선임' in children[0]['title'] else '이사 선임의 건'
            result.append({
                "no": pno, "title": virtual_title, "type": "보통결의(추정)",
                "is_sub": False, "parent_no": None, "candidate": "", "virtual": True,
            })
            result.extend(children)

    return result if result else items


def fetch_dart_agendas(company_name: str, api_key: str) -> tuple[list, str]:
    """
    의결권대리행사권유참고서류에서 의안 목록 조회 (계층 구조).
    캐시 키: company_name + 연도 → 매년 자동 갱신.
    Returns (agendas: list[dict], message: str)
    """
    if not api_key:
        return [], "API 키 없음"
    if not HAS_ODR:
        return [], "OpenDartReader 미설치"

    year      = datetime.now().year
    cache_key = f"{company_name}_{year}"

    # 파일 캐시 확인 (연도 포함 키)
    cache = _load_agenda_cache()
    if cache_key in cache:
        cached = cache[cache_key]
        if cached.get("agendas") is not None:
            return cached["agendas"], f"저장됨 ({cached.get('fetched_at','')[:10]})"

    try:
        corp_dict = load_corp_codes(api_key)
        corp_code = find_corp_code(corp_dict, company_name)
        if not corp_code:
            return [], "기업코드 미발견"

        dart    = OpenDartReader(api_key)
        reports = dart.list(corp_code, start=f"{year}-01-01", end=f"{year}-03-31")

        if not isinstance(reports, pd.DataFrame) or reports.empty:
            return [], "공시 없음"

        # 우선순위: 의결권대리행사권유참고서류 → 신고서 → 소집공고 → 소집결의
        target = None
        for keyword in [
            "의결권대리행사권유참고서류",
            "의결권대리행사권유신고서",
            "주주총회소집공고",
            "주주총회소집결의",
        ]:
            mask = reports["report_nm"].str.contains(keyword, na=False)
            if mask.any():
                target    = reports[mask].iloc[0]
                report_nm = target["report_nm"]
                rcept_no  = target["rcept_no"]
                break

        if target is None:
            return [], "의안 공시 없음"

        # ── 문서 원문 가져오기 (OpenDartReader → 직접 API fallback) ──
        text = None
        fetch_err = ""
        try:
            doc_html = dart.document(rcept_no)
            soup     = BeautifulSoup(doc_html, "html.parser")
            text     = soup.get_text(separator="\n")
        except Exception as e1:
            fetch_err = str(e1)

        # Fallback: OpenDartReader 실패 시 직접 ZIP 다운로드
        if text is None:
            try:
                r = requests.get(
                    "https://opendart.fss.or.kr/api/document.xml",
                    params={"crtfc_key": api_key, "rcept_no": rcept_no},
                    timeout=20)
                r.raise_for_status()
                zf       = zipfile.ZipFile(io.BytesIO(r.content))
                doc_names = [f for f in zf.namelist()
                             if f.lower().endswith((".htm",".html",".xml"))]
                if not doc_names:
                    doc_names = zf.namelist()
                raw_html = "".join(
                    zf.read(n).decode("utf-8", errors="ignore") for n in doc_names)
                soup = BeautifulSoup(raw_html, "html.parser")
                text = soup.get_text(separator="\n")
            except Exception as e2:
                return [], f"문서오류: {fetch_err[:40]} / {str(e2)[:40]}"

        if not text:
            return [], f"빈 문서: {fetch_err[:60]}"

        raw     = _parse_agendas(text)
        agendas = _group_agendas(raw)

        # 파일 캐시 저장 (연도 포함 키)
        cache[cache_key] = {
            "agendas":    agendas,
            "report_nm":  report_nm,
            "year":       year,
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        _save_agenda_cache(cache)

        if agendas:
            return agendas, f"DART ({report_nm[:20]})"
        return [], f"파싱실패: {report_nm[:20]} (의안 패턴 없음)"

    except requests.exceptions.ConnectionError as e:
        return [], f"네트워크 오류: {str(e)[:80]}"
    except Exception as e:
        return [], f"오류({type(e).__name__}): {str(e)[:80]}"

def search_via_claude(company_name: str, anthropic_key: str):
    """
    Claude API + web_search 툴로 주총 날짜 검색.
    실제 채팅에서 하는 방식과 동일 — 뉴스/공시 텍스트에서 날짜를 자연어로 읽어옴.
    Returns (날짜|None, 메시지)
    """
    if not anthropic_key:
        return None, "Anthropic 키 없음"
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": anthropic_key,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "web-search-2025-03-05",  # 웹검색 베타 헤더
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 256,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{
                    "role": "user",
                    "content": (
                        f"{company_name}의 2026년 정기주주총회 날짜를 검색해서 "
                        f"YYYY-MM-DD 형식으로만 답해줘. "
                        f"날짜를 모르면 '모름'이라고만 해."
                    )
                }],
            },
            timeout=30,
        )

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("retry-after", 60))
            return None, f"rate limit (잠시 후 재시도, {retry_after}초)"
        resp.raise_for_status()

        data = resp.json()
        # content 블록에서 텍스트 추출
        full_text = " ".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )

        # 2026-03-XX 패턴 추출
        m = re.search(r"2026-03-([0-2]\d|3[01])", full_text)
        if m:
            return m.group(0), "AI 웹검색"

        # "3월 XX일" 패턴도 처리
        m2 = re.search(r"3월\s*([0-2]?\d|3[01])일", full_text)
        if m2:
            d = f"2026-03-{int(m2.group(1)):02d}"
            if validate_march_2026(d):
                return d, "AI 웹검색"

        if "모름" in full_text or not full_text.strip():
            return None, "AI: 날짜 미확인"
        return None, f"AI: 파싱실패 ({full_text[:30]})"

    except requests.exceptions.ConnectionError:
        return None, "AI: 네트워크 오류"
    except Exception as e:
        return None, f"AI 오류: {str(e)[:40]}"


# ── 달력 ─────────────────────────────────────

def build_day_map(df: pd.DataFrame, state: dict) -> dict:
    day_map  = {}
    overrides    = state["overrides"]
    updated      = state.get("updated_recently", set())
    done_status  = state.get("done_status", {})
    for _, row in df.iterrows():
        company = row["단체명"]
        disp    = overrides.get(company, row["주주총회일"])
        conf    = is_confirmed(disp)
        key     = disp[:10] if conf else extract_pending_date(row["주주총회일"])
        if not key: continue
        day_map.setdefault(key, []).append({
            "name":     company,
            "confirmed": conf,
            "required":  row["비고"] == "필수단체",
            "updated":   company in updated,
            "manager":   row.get("운용사", ""),
            "done":      bool(done_status.get(company, False)),
        })
    return day_map


def render_calendar_html(year: int, month: int, day_map: dict) -> str:
    today_str = date.today().strftime("%Y-%m-%d")
    html = ['<div class="cal-wrap"><table class="cal"><tr>']
    for wd in ["월","화","수","목","금"]:
        html.append(f"<th>{wd}</th>")
    html.append('<th class="week-col">주간<br>합계</th></tr>')

    for week in calendar.monthcalendar(year, month):
        wd = week[:5]
        if all(d==0 for d in wd): continue
        wc = wp = 0
        for d in wd:
            if not d: continue
            for it in day_map.get(f"{year}-{month:02d}-{d:02d}", []):
                if it["confirmed"]: wc+=1
                else: wp+=1
        html.append("<tr>")
        for d in wd:
            if not d:
                html.append('<td class="empty"></td>'); continue
            key   = f"{year}-{month:02d}-{d:02d}"
            items = day_map.get(key, [])
            total = len(items)
            conf_n = sum(1 for i in items if i["confirmed"])
            badge  = ""
            if total:
                bc = "day-badge" + (" has-pending" if conf_n==0 else "")
                badge = f'<span class="{bc}">{total}</span>'
            td_cls = "today" if key==today_str else ""
            cell = f'<td class="{td_cls}"><div class="cal-day-num">{d}{badge}</div>'
            for it in sorted(items, key=lambda x:(not x["confirmed"],not x["required"])):
                if it.get("done"):
                    cls = "chip chip-done req" if it["required"] else "chip chip-done"
                elif it["updated"]:
                    cls = "chip chip-updated"
                elif it["confirmed"] and it["required"]:
                    cls = "chip chip-confirmed req"
                elif it["confirmed"]:
                    cls = "chip chip-confirmed"
                elif it["required"]:
                    cls = "chip chip-pending req"
                else:
                    cls = "chip chip-pending"
                pfx   = "★" if it["required"] else ""
                sfx   = "" if it["confirmed"] else " *"
                done_sfx = " ✓" if it.get("done") else ""
                mgr   = it.get("manager","")
                title = f' title="{mgr}"' if mgr else ""
                cell += f'<span class="{cls}"{title}>{pfx}{it["name"]}{sfx}{done_sfx}</span>'
            cell += "</td>"
            html.append(cell)
        if wc+wp:
            html.append(f'<td class="week-total"><div class="week-cnt">🗓 {wc+wp}</div>'
                        f'<div class="week-sub">확정 {wc}<br>미정 {wp}</div></td>')
        else:
            html.append('<td class="week-total"><span style="color:#ccc">—</span></td>')
        html.append("</tr>")
    html.append("</table></div>")
    return "\n".join(html)


# ── 사이드바 ──────────────────────────────────

def _pending_only(df, state) -> pd.DataFrame:
    """날짜가 미정인 기업만 반환"""
    overrides = state["overrides"]
    mask = df["단체명"].apply(
        lambda c: not is_confirmed(overrides.get(c, df.loc[df["단체명"]==c, "주주총회일"].iloc[0]))
    )
    return df[mask].reset_index(drop=True)


def _run_bulk_search(df, state, search_fn, label, delay=1.5):
    """공통 전체검색 루프 — 결과를 pending_updates에 저장 (미적용 상태)"""
    corps    = df["단체명"].tolist()
    total    = len(corps)
    prog     = st.sidebar.progress(0)
    results  = st.session_state.get("crawl_results", {})
    pending  = st.session_state.get("pending_updates", {})
    upd_list = []; unc_list = []; nor_list = []

    for i, corp in enumerate(corps):
        prog.progress((i + 1) / total, text=f"[{i+1}/{total}] {corp}…")
        found, src = search_fn(corp)
        results[corp] = {"date": found, "source": src}

        if validate_march_2026(found):
            r_row = df[df["단체명"] == corp]
            cur = state["overrides"].get(
                corp, r_row.iloc[0]["주주총회일"] if not r_row.empty else "")
            if found != cur:
                pending[corp] = {"new_date": found, "source": src, "prev_date": cur}
                upd_list.append((corp, cur, found, src))
            else:
                unc_list.append((corp, found))
        else:
            nor_list.append((corp, src))
        time.sleep(delay)

    prog.empty()
    st.session_state["crawl_results"] = results
    st.session_state["pending_updates"] = pending
    st.session_state["search_log"] = {
        "label":     label,
        "updated":   upd_list,
        "unchanged": unc_list,
        "no_result": nor_list,
        "ran_at":    datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    st.rerun()



# ── GitHub 동기화 ─────────────────────────────

def sync_to_github(state: dict, gh_token: str, repo_name: str, file_path: str = "주주총회.xlsx"):
    """
    overrides 적용된 내용으로 xlsx 수정 후 GitHub repo에 push.
    Returns (success: bool, message: str)
    """
    if not HAS_GITHUB:
        return False, "PyGithub 미설치 (pip install PyGithub)"
    try:
        import openpyxl
        from copy import copy

        # ── xlsx 수정 ──
        wb = openpyxl.load_workbook(EXCEL_PATH)
        ws = wb["리스트 대상 운용사"]

        overrides       = state.get("overrides", {})
        name_replacements = state.get("name_replacements", {})

        # 헤더가 2행(header=1 → row index 2), 데이터는 3행부터
        # 컬럼: B=단체명, C=주주총회일, D=비고
        updated_count = 0
        for row in ws.iter_rows(min_row=3):
            name_cell = row[1]   # col B (0-indexed: B=1)
            date_cell = row[2]   # col C
            if name_cell.value is None:
                continue
            corp = str(name_cell.value).strip()
            # 기업명 교체 반영
            new_corp = name_replacements.get(corp, corp)
            if new_corp != corp:
                name_cell.value = new_corp
                corp = new_corp
                updated_count += 1
            # 날짜 override 반영
            if corp in overrides:
                new_d = overrides[corp]
                if re.match(r"\d{4}-\d{2}-\d{2}", str(new_d)):
                    date_cell.value = datetime.strptime(new_d, "%Y-%m-%d")
                    updated_count += 1

        # 수정된 xlsx를 메모리 버퍼에 저장
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        new_content = buf.read()

        # ── GitHub push ──
        g    = Github(gh_token)
        repo = g.get_repo(repo_name)

        try:
            existing = repo.get_contents(file_path)
            repo.update_file(
                path    = file_path,
                message = f"주총일정 업데이트 ({datetime.now().strftime('%Y-%m-%d %H:%M')})",
                content = new_content,
                sha     = existing.sha,
            )
        except GithubException as e:
            if e.status == 404:
                repo.create_file(
                    path    = file_path,
                    message = f"주총일정 최초 업로드 ({datetime.now().strftime('%Y-%m-%d %H:%M')})",
                    content = new_content,
                )
            else:
                raise

        # ── agm_state.json도 GitHub에 push ──
        state_out = dict(state)
        state_out["updated_recently"] = list(state.get("updated_recently", set()))
        state_bytes = json.dumps(state_out, ensure_ascii=False, indent=2).encode("utf-8")
        state_gh_path = GH_STATE_PATH

        try:
            existing_state = repo.get_contents(state_gh_path)
            repo.update_file(
                path    = state_gh_path,
                message = f"상태 저장 ({datetime.now().strftime('%Y-%m-%d %H:%M')})",
                content = state_bytes,
                sha     = existing_state.sha,
            )
        except GithubException as e2:
            if e2.status == 404:
                repo.create_file(
                    path    = state_gh_path,
                    message = f"상태 최초 저장 ({datetime.now().strftime('%Y-%m-%d %H:%M')})",
                    content = state_bytes,
                )
            else:
                raise

        return True, f"✅ GitHub 동기화 완료 (xlsx {updated_count}건 + 상태 저장)"

    except Exception as e:
        return False, f"❌ 오류: {str(e)[:120]}"

def _pull_state_from_github(gh_token: str, repo_name: str) -> bool:
    """
    앱 시작 시 로컬 agm_state.json이 없으면 GitHub에서 pull.
    Returns True if successfully restored.
    """
    if not HAS_GITHUB or not gh_token or not repo_name:
        return False
    if os.path.exists(STATE_PATH):
        return False   # 로컬 파일 있으면 그냥 사용
    try:
        g    = Github(gh_token)
        repo = g.get_repo(repo_name)
        f    = repo.get_contents(GH_STATE_PATH)
        raw  = f.decoded_content.decode("utf-8")
        with open(STATE_PATH, "w", encoding="utf-8") as fp:
            fp.write(raw)
        return True
    except Exception:
        return False


def _save_and_push_state(state: dict, gh_token: str = "", repo_name: str = ""):
    """
    로컬 저장 + (토큰 있으면) GitHub에 즉시 push.
    save_state() 대신 이걸 쓰면 체크 즉시 GitHub에 반영.
    """
    # 로컬 저장
    out = dict(state)
    out["updated_recently"] = list(state.get("updated_recently", set()))
    json.dump(out, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # GitHub push (토큰 있을 때만)
    if HAS_GITHUB and gh_token and repo_name:
        try:
            g    = Github(gh_token)
            repo = g.get_repo(repo_name)
            content_bytes = json.dumps(out, ensure_ascii=False, indent=2).encode("utf-8")
            try:
                existing = repo.get_contents(GH_STATE_PATH)
                repo.update_file(
                    path    = GH_STATE_PATH,
                    message = f"상태 자동저장 ({datetime.now().strftime('%Y-%m-%d %H:%M')})",
                    content = content_bytes,
                    sha     = existing.sha,
                )
            except GithubException as e:
                if e.status == 404:
                    repo.create_file(
                        path    = GH_STATE_PATH,
                        message = "상태 최초 저장",
                        content = content_bytes,
                    )
        except Exception:
            pass   # push 실패해도 로컬은 저장됨


def render_sidebar(state: dict):
    st.sidebar.title("⚙️ 설정")

    # ── DART API 키 (무료) ──
    dart_key = st.sidebar.text_input(
        "DART OpenAPI 키 (무료)",
        type="password",
        help="opendart.fss.or.kr → 회원가입 → API 신청. 완전 무료."
    )
    if dart_key:
        if os.path.exists(CORP_CACHE):
            age_h = (time.time() - os.path.getmtime(CORP_CACHE)) / 3600
            st.sidebar.caption(f"✅ 기업코드 캐시 ({age_h:.0f}시간 전)")
            if st.sidebar.button("🔄 기업코드 갱신"):
                os.remove(CORP_CACHE)
                try: load_corp_codes(dart_key); st.sidebar.success("완료")
                except Exception as e: st.sidebar.error(str(e))
        else:
            st.sidebar.caption("⚠️ 첫 검색 시 자동 다운로드")

    # ── Anthropic API 키 (유료) ──
    st.sidebar.markdown("")
    anthropic_key = st.sidebar.text_input(
        "Anthropic API 키 (AI 웹검색, 유료)",
        type="password",
        help="console.anthropic.com에서 발급. 미정 기업만 검색 시 수십 센트 수준."
    )

    st.sidebar.markdown("---")

    # ── 미정만 검색 옵션 ──
    df_all = load_excel_data()
    df_pending = _pending_only(df_all, state)
    pending_n  = len(df_pending)
    total_n    = len(df_all)

    pending_only = st.sidebar.checkbox(
        f"⏳ 미정 기업만 검색 ({pending_n}개 / 전체 {total_n}개)",
        value=True,
        help="날짜가 확정되지 않은 기업만 검색합니다. 비용과 시간을 절약할 수 있어요."
    )
    df_target = df_pending if pending_only else df_all

    if pending_only and pending_n == 0:
        st.sidebar.success("🎉 모든 기업의 날짜가 확정되었습니다!")

    st.sidebar.markdown("---")

    # ── AI 웹검색 버튼 (유료지만 미정만 하면 저렴) ──
    if anthropic_key and pending_n > 0:
        est_cost = pending_n * 0.015 if pending_only else total_n * 0.015
        st.sidebar.caption(f"예상 비용: 약 ${est_cost:.2f} ({len(df_target)}개 기업)")
        if st.sidebar.button("🤖 AI 웹검색", use_container_width=True, type="primary"):
            _run_bulk_search(
                df_target, state,
                search_fn=lambda corp: search_via_claude(corp, anthropic_key),
                label=f"AI 웹검색 ({'미정만' if pending_only else '전체'})",
                delay=1.5,
            )

    # ── DART API 버튼 (무료) ──
    if dart_key:
        if st.sidebar.button("📡 DART API 검색 (무료)", use_container_width=True,
                             type="primary" if not anthropic_key else "secondary"):
            try:
                with st.spinner("기업코드 로딩…"): load_corp_codes(dart_key)
            except Exception as e:
                st.sidebar.error(str(e))
            else:
                _run_bulk_search(
                    df_target, state,
                    search_fn=lambda corp: search_dart_api(corp, dart_key),
                    label=f"DART API ({'미정만' if pending_only else '전체'})",
                    delay=0.3,
                )

    if not dart_key and not anthropic_key:
        st.sidebar.info("**DART API 키**는 무료입니다.\n"
                        "opendart.fss.or.kr에서 바로 발급 가능.\n\n"
                        "키 없이는 리스트 탭 ✏️ 날짜입력으로 수동 등록하세요.")

    st.sidebar.markdown("---")

    # ── 업데이트 사항 전체 적용 ──
    pending  = st.session_state.get("pending_updates", {})
    selected = st.session_state.get("apply_selected", set())
    to_apply = [c for c in selected if c in pending]
    if to_apply:
        st.sidebar.caption(f"✅ 체크된 항목: {len(to_apply)}개 / 미적용 {len(pending)}개")
        if st.sidebar.button(f"✅ 업데이트 사항 전체 적용 ({len(to_apply)}개)",
                             use_container_width=True, type="primary"):
            for corp in to_apply:
                new_d = pending[corp]["new_date"]
                if validate_march_2026(new_d):
                    state["overrides"][corp] = new_d
                    state.setdefault("updated_recently", set()).add(corp)
                    state.setdefault("updated_timestamps", {})[corp] = datetime.now().isoformat()
            # 적용된 항목을 pending에서 제거
            for corp in to_apply:
                pending.pop(corp, None)
                selected.discard(corp)
            st.session_state["pending_updates"] = pending
            st.session_state["apply_selected"]  = selected
            save_state(state)
            st.sidebar.success(f"{len(to_apply)}개 적용 완료!")
            st.rerun()
    elif pending:
        st.sidebar.caption(f"⏳ 미적용 항목: {len(pending)}개 (리스트에서 체크하세요)")

    if st.sidebar.button("🗑️ 업데이트 표시 초기화", use_container_width=True):
        state["updated_recently"] = set(); save_state(state); st.rerun()
    if st.sidebar.button("⚠️ 전체 초기화", use_container_width=True, type="secondary"):
        for p in [STATE_PATH, CORP_CACHE]:
            if os.path.exists(p): os.remove(p)
        st.session_state["state"] = load_state(); st.rerun()

    st.sidebar.markdown("---")

    # ── GitHub 동기화 ──
    st.sidebar.markdown("**🐙 GitHub 동기화**")
    with st.sidebar.expander("⚠️ 토큰 발급 방법 (Fine-grained)", expanded=False):
        st.markdown("""
1. github.com → Settings  
2. Developer settings → **Personal access tokens → Fine-grained tokens**  
3. Generate new token  
4. **Repository access** → Only select repositories → 해당 repo 선택  
5. **Permissions → Contents → Read and write** ✅  
6. Generate → 토큰 복사 후 아래에 붙여넣기

> ⚠️ Classic token은 조직 repo에서 403 오류가 날 수 있어요.
""")
    gh_token = st.sidebar.text_input(
        "GitHub Token (Fine-grained 권장)",
        type="password",
        help="Fine-grained: Contents Read&Write 권한 필요",
        key="gh_token_input",
    )
    gh_repo = st.sidebar.text_input(
        "Repository (user/repo 형식)",
        placeholder="예: yourname/agm-tracker",
        help="xlsx 파일이 있는 GitHub 레포지토리 경로",
        key="gh_repo_input",
    )
    gh_path = st.sidebar.text_input(
        "파일 경로 (repo 내 경로)",
        value="주주총회.xlsx",
        help="repo 루트에 있으면 파일명만, 폴더 안이면 folder/파일명.xlsx",
        key="gh_path_input",
    )
    # 토큰·레포 session_state에 보관 (save/push 함수에서 참조)
    if gh_token: st.session_state["_gh_token"] = gh_token
    if gh_repo:  st.session_state["_gh_repo"]  = gh_repo

    if not HAS_GITHUB:
        st.sidebar.warning("PyGithub 미설치 — `pip install PyGithub`")
    elif gh_token and gh_repo:
        overrides_count = len(state.get("overrides", {}))
        st.sidebar.caption(f"적용된 날짜 override: {overrides_count}건")
        done_count   = sum(1 for v in state.get("done_status",{}).values() if v)
        agenda_count = sum(1 for v in state.get("agenda_status",{}).values() if v)
        st.sidebar.caption(
            f"날짜 override {len(state.get('overrides',{}))}건 │ "
            f"의안분석 {agenda_count}건 │ 완료 {done_count}건 저장됨")
        if st.sidebar.button("🔄 GitHub 동기화 (xlsx + 상태저장)", use_container_width=True, type="primary"):
            with st.spinner("GitHub에 업로드 중…"):
                ok, msg = sync_to_github(state, gh_token, gh_repo, gh_path)
            if ok:
                st.sidebar.success(msg)
            else:
                # 403이면 구체적인 안내
                if "403" in msg or "not accessible" in msg.lower():
                    st.sidebar.error("❌ 권한 오류 (403)")
                    st.sidebar.info(
                        "**해결 방법:**\n"
                        "Classic token → Fine-grained token으로 재발급\n"
                        "Permissions → Contents → **Read and write** 설정 필요"
                    )
                else:
                    st.sidebar.error(msg)
    else:
        st.sidebar.caption("토큰과 레포지토리를 입력하면 버튼이 활성화됩니다.")

    st.sidebar.markdown("---")
    st.sidebar.markdown("""
**달력 범례**  
🟢 확정 &nbsp;🔵 확정+필수  
🟡 업데이트 &nbsp;🟠 점선=미정  
★ 필수단체 &nbsp;* 미정
""")
    return dart_key, anthropic_key


# ── 검색 결과 로그 ────────────────────────────

def render_search_log():
    log = st.session_state.get("search_log")
    if not log: return
    upd = log["updated"]; unc = log["unchanged"]; nor = log["no_result"]

    with st.expander(
        f"🔎 DART 검색 완료 ({log['ran_at']}) │ "
        f"업데이트 **{len(upd)}건** / 변경없음 {len(unc)}건 / 미조회 {len(nor)}건",
        expanded=True
    ):
        if st.button("✖ 닫기", key="close_log"):
            st.session_state["search_log"] = None; st.rerun()

        if upd:
            st.markdown("#### 📢 업데이트된 기업")
            st.dataframe(pd.DataFrame(
                [{"기업":c,"이전":o or "미정","→ 새 날짜":n,"출처":s} for c,o,n,s in upd]),
                use_container_width=True, hide_index=True)
        else:
            st.info("업데이트된 기업이 없습니다.")

        if unc:
            with st.expander(f"변경 없음 ({len(unc)}건)"):
                st.dataframe(pd.DataFrame([{"기업":c,"날짜":d} for c,d in unc]),
                    use_container_width=True, hide_index=True)
        if nor:
            with st.expander(f"미조회 ({len(nor)}건)"):
                st.dataframe(pd.DataFrame([{"기업":c,"사유":s} for c,s in nor]),
                    use_container_width=True, hide_index=True)


# ── 리스트 뷰 ─────────────────────────────────

# 컬럼 너비: 기업명, 주총일자, 운용사, 날짜체크, 날짜입력, 기업변경, 의안분석, 진행완료
_COL_W = [3.0, 2.0, 2.2, 1.3, 1.3, 1.3, 1.5, 1.5]


def _apply_company_change(state, old_name, new_name, new_date, orig_date, new_manager=""):
    """기업변경 처리 + change_history 기록"""
    ch = state.setdefault("change_history", {})
    prev_entry = {
        "prev_name":  old_name,
        "prev_date":  state["overrides"].get(old_name, orig_date),
        "changed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    existing = ch.get(old_name, [])
    ch[new_name] = existing + [prev_entry]
    ch.pop(old_name, None)
    state["overrides"].pop(old_name, None)
    state["overrides"][new_name] = new_date
    state.setdefault("name_replacements", {})[old_name] = new_name
    state.setdefault("changes", {})[new_name] = prev_entry
    # 운용사 override
    if new_manager:
        state.setdefault("manager_overrides", {})[new_name] = new_manager


def render_list_view(df: pd.DataFrame, state: dict, dart_key: str, anthropic_key: str = ""):
    overrides        = state["overrides"]
    manager_ov       = state.setdefault("manager_overrides", {})
    change_history   = state.setdefault("change_history", {})
    updated_recently = state.get("updated_recently", set())
    agenda_status    = state.setdefault("agenda_status", {})
    done_status      = state.setdefault("done_status", {})
    pending          = st.session_state.setdefault("pending_updates", {})
    apply_sel        = st.session_state.setdefault("apply_selected", set())

    df = df.copy()
    df["_disp"] = df.apply(lambda r: overrides.get(r["단체명"], r["주주총회일"]), axis=1)
    df["_conf"] = df["_disp"].apply(is_confirmed)

    # ── 정렬 상태 ──
    sort_by  = st.session_state.get("list_sort_by", "date")    # "date"|"name"
    sort_dir = st.session_state.get("list_sort_dir", "asc")    # "asc"|"desc"

    def _sort_df(d):
        if sort_by == "name":
            d = d.sort_values("단체명", ascending=(sort_dir=="asc"))
        else:
            def _key(r):
                disp = r["_disp"]
                if is_confirmed(disp):
                    return (0, disp, r["단체명"])
                return (1, extract_pending_date(disp) or "9999-12-31", r["단체명"])
            d = d.assign(_sk=d.apply(_key, axis=1)).sort_values("_sk").drop(columns=["_sk"])
            if sort_dir == "desc":
                # 확정은 내림차순, 미정은 항상 뒤
                conf  = d[d["_conf"]].iloc[::-1].reset_index(drop=True)
                pend  = d[~d["_conf"]].reset_index(drop=True)
                d = pd.concat([conf, pend], ignore_index=True)
        return d.reset_index(drop=True)

    df = _sort_df(df)

    total_conf   = int(df["_conf"].sum())
    total_pend   = len(df) - total_conf
    pend_cnt     = len(pending)
    changed_corps = set(change_history.keys())
    required_corps = set(df[df["비고"] == "필수단체"]["단체명"].tolist())

    # ── 요약 + 필터 버튼 행 ──
    sc1, sc2, sc3, sc4, sc5 = st.columns([4, 2, 2, 2, 2])
    with sc1:
        st.markdown(
            f'<span style="font-size:.9em;color:#6b7280">총 {len(df)}개 │ '
            f'<span style="color:#166534">확정 {total_conf}개</span> │ '
            f'<span style="color:#9a3412">미정 {total_pend}개</span>'
            + (f' │ <span style="color:#d97706">미적용 {pend_cnt}개</span>' if pend_cnt else "")
            + "</span>", unsafe_allow_html=True)
    with sc2:
        show_changed = st.session_state.get("filter_changed_only", False)
        changed_n    = len(changed_corps)
        if changed_n:
            lbl = f"{'✅' if show_changed else '🔲'} 변경기업 ({changed_n})"
            if st.button(lbl, use_container_width=True):
                st.session_state["filter_changed_only"] = not show_changed
                st.session_state.pop("filter_required_only", None)
                st.rerun()
        else:
            st.caption("변경기업 없음")
    with sc3:
        show_req  = st.session_state.get("filter_required_only", False)
        req_n     = len(required_corps)
        lbl_req   = f"{'✅' if show_req else '🔲'} 필수기업 ({req_n})"
        if st.button(lbl_req, use_container_width=True):
            st.session_state["filter_required_only"] = not show_req
            st.session_state.pop("filter_changed_only", None)
            st.rerun()
    with sc4:
        if pend_cnt:
            if st.button(f"☑ 전체선택 ({pend_cnt})", use_container_width=True):
                st.session_state["apply_selected"] = set(pending.keys())
                st.rerun()
    with sc5:
        if apply_sel:
            if st.button("☐ 선택해제", use_container_width=True):
                st.session_state["apply_selected"] = set()
                st.rerun()

    # 필터 적용
    show_changed = st.session_state.get("filter_changed_only", False)
    show_req     = st.session_state.get("filter_required_only", False)
    if show_changed and changed_corps:
        df = df[df["단체명"].isin(changed_corps)].reset_index(drop=True)
    elif show_req:
        df = df[df["비고"] == "필수단체"].reset_index(drop=True)

    df_conf = df[df["_conf"]].reset_index(drop=True)
    df_pend = df[~df["_conf"]].reset_index(drop=True)

    # ── 헤더 렌더 (정렬 버튼 포함) ──
    def _render_header(prefix=""):
        hcols = st.columns(_COL_W)
        hcss_base = ("color:#fff;font-weight:700;font-size:.78em;"
                     "padding:5px 3px;text-align:center;border-radius:3px;")
        # 열1: 기업명 (정렬 버튼)
        with hcols[0]:
            arr = ("↑" if sort_dir=="asc" else "↓") if sort_by=="name" else "↕"
            if st.button(f"정기주총 의결권행사기업 {arr}",
                         key=f"sort_name_{prefix}", use_container_width=True,
                         help="이름 오름차순/내림차순"):
                if sort_by == "name":
                    st.session_state["list_sort_dir"] = "desc" if sort_dir=="asc" else "asc"
                else:
                    st.session_state["list_sort_by"]  = "name"
                    st.session_state["list_sort_dir"] = "asc"
                st.rerun()
        # 열2: 주총일자 (정렬 버튼)
        with hcols[1]:
            arr = ("↑" if sort_dir=="asc" else "↓") if sort_by=="date" else "↕"
            if st.button(f"주총일자 {arr}",
                         key=f"sort_date_{prefix}", use_container_width=True,
                         help="날짜 오름차순/내림차순"):
                if sort_by == "date":
                    st.session_state["list_sort_dir"] = "desc" if sort_dir=="asc" else "asc"
                else:
                    st.session_state["list_sort_by"]  = "date"
                    st.session_state["list_sort_dir"] = "asc"
                st.rerun()
        # 나머지 고정 헤더
        static = ["운용사", "날짜체크", "날짜입력", "기업변경", "의안분석\n현황", "진행완료\n여부"]
        for col, h in zip(hcols[2:], static):
            with col:
                st.markdown(f'<div style="background:#1e3a5f;{hcss_base}">{h}</div>',
                            unsafe_allow_html=True)

    # ── 행 렌더링 ──
    def _render_rows(sub_df):
        for _, row in sub_df.iterrows():
            company     = row["단체명"]
            orig        = row["주주총회일"]
            disp        = overrides.get(company, orig)
            required    = row["비고"] == "필수단체"
            manager     = manager_ov.get(company, row.get("운용사", ""))
            updated     = company in updated_recently
            hist        = change_history.get(company, [])
            hist_n      = len(hist)
            hist_open   = company in st.session_state["change_history_open"]
            has_pending = company in pending
            confirmed   = is_confirmed(disp)
            agenda_on   = agenda_status.get(company, False)
            done_on     = done_status.get(company, False)

            st.markdown('<hr style="margin:1px 0;border:none;border-top:1px solid #e2e8f0">',
                        unsafe_allow_html=True)
            cols = st.columns(_COL_W)

            # 열1: 기업명
            agenda_open_set = st.session_state["agenda_open"]
            agenda_open_co  = company in agenda_open_set
            with cols[0]:
                req_sfx  = " 🔴" if required else ""
                upd_html = ' <span class="updated-badge">🔄</span>' if updated else ""
                st.markdown(
                    f'<div style="font-weight:600;font-size:1.0em;line-height:1.4">'
                    f'{company}{req_sfx}{upd_html}</div>', unsafe_allow_html=True)

                # 의안 드롭다운 버튼
                if dart_key:
                    agenda_lbl = f"{'▼' if agenda_open_co else '▶'} 의안조회"
                    if st.button(agenda_lbl, key=f"ag_tog_{company}",
                                 help="의결권대리행사권유참고서류 의안 조회"):
                        if agenda_open_co:
                            agenda_open_set.discard(company)
                        else:
                            agenda_open_set.add(company)
                            mem_cache  = st.session_state["agenda_cache_mem"]
                            _year      = datetime.now().year
                            _cache_key = f"{company}_{_year}"
                            if _cache_key not in mem_cache:
                                with st.spinner(f"{company} 의안 조회 중…"):
                                    _items, _msg = fetch_dart_agendas(company, dart_key)
                                mem_cache[_cache_key] = {"items": _items, "msg": _msg}
                                st.session_state["agenda_cache_mem"] = mem_cache
                        st.rerun()

                if hist_n:
                    lbl = f"{'▼' if hist_open else '▶'} {hist_n}회 변경"
                    if st.button(lbl, key=f"hist_{company}", help="변경 히스토리"):
                        if hist_open: st.session_state["change_history_open"].discard(company)
                        else:         st.session_state["change_history_open"].add(company)
                        st.rerun()
                if has_pending:
                    pd_info    = pending[company]
                    is_checked = company in apply_sel
                    new_check  = st.checkbox(
                        f"업데이트 적용 → {pd_info['new_date'][5:]}",
                        value=is_checked, key=f"apply_{company}")
                    if new_check != is_checked:
                        if new_check: apply_sel.add(company)
                        else:         apply_sel.discard(company)
                        st.session_state["apply_selected"] = apply_sel
                        st.rerun()

            # 열2: 날짜
            with cols[1]:
                if confirmed:
                    st.markdown(f'<span class="date-conf" style="font-size:.88em">{disp}</span>',
                                unsafe_allow_html=True)
                else:
                    est     = extract_pending_date(orig)
                    est_txt = (f"<br><span style='font-size:.75em;color:#6b7280'>예상 {est}</span>"
                               if est else "")
                    st.markdown(
                        f'<span class="date-pend" style="font-size:.85em">{disp}</span>{est_txt}',
                        unsafe_allow_html=True)

            # 열3: 운용사
            with cols[2]:
                st.markdown(
                    f'<span style="font-size:.85em;color:#374151">{manager}</span>',
                    unsafe_allow_html=True)

            # 열4: 날짜체크
            with cols[3]:
                can_search = anthropic_key or dart_key
                if can_search:
                    tip = "AI 웹검색" if anthropic_key else "DART API 검색"
                    if st.button("🔍", key=f"srch_{company}", help=tip):
                        with st.spinner(f"{company}…"):
                            if anthropic_key:
                                found, src = search_via_claude(company, anthropic_key)
                            else:
                                found, src = search_dart_api(company, dart_key)
                        cr = st.session_state.get("crawl_results", {})
                        cr[company] = {"date": found, "source": src}
                        st.session_state["crawl_results"] = cr
                        if validate_march_2026(found) and found != disp:
                            pending[company] = {"new_date": found, "source": src, "prev_date": disp}
                            st.session_state["pending_updates"] = pending
                        st.rerun()
                if company in pending:
                    nd = pending[company]["new_date"]
                    st.markdown(
                        f'<span style="color:#166534;font-weight:700;font-size:.85em">→ {nd[5:]}</span>',
                        unsafe_allow_html=True)
                else:
                    cr = st.session_state.get("crawl_results", {})
                    if company in cr:
                        fd = cr[company].get("date")
                        if validate_march_2026(fd) and fd == disp:
                            st.markdown('<span class="src-same" style="font-size:.8em">✓ 동일</span>',
                                        unsafe_allow_html=True)
                        elif not validate_march_2026(fd):
                            st.markdown('<span class="src-err" style="font-size:.8em">✗</span>',
                                        unsafe_allow_html=True)

            # 열5: 날짜입력
            with cols[4]:
                manual_active = st.session_state.get("inline_manual") == company
                if st.button("✖" if manual_active else "✏️", key=f"man_{company}",
                             use_container_width=True,
                             help="닫기" if manual_active else "날짜 직접 입력"):
                    st.session_state["inline_manual"] = None if manual_active else company
                    st.session_state["inline_change"]  = None
                    st.rerun()

            # 열6: 기업변경
            with cols[5]:
                change_active = st.session_state.get("inline_change") == company
                if st.button("✖" if change_active else "🔄", key=f"chg_{company}",
                             use_container_width=True,
                             help="취소" if change_active else "기업 교체"):
                    st.session_state["inline_change"]  = None if change_active else company
                    st.session_state["inline_manual"]  = None
                    st.rerun()

            # 열7: 의안분석 현황
            with cols[6]:
                lbl = "🟢 O" if agenda_on else "—"
                if st.button(lbl, key=f"agenda_{company}", use_container_width=True):
                    new_val = not agenda_on
                    state["agenda_status"][company] = new_val
                    st.session_state["state"]["agenda_status"][company] = new_val
                    _save_and_push_state(
                        st.session_state["state"],
                        st.session_state.get("_gh_token",""),
                        st.session_state.get("_gh_repo",""),
                    )
                    st.rerun()

            # 열8: 진행완료 여부
            with cols[7]:
                lbl = "✅ O" if done_on else "—"
                if st.button(lbl, key=f"done_{company}", use_container_width=True):
                    new_val = not done_on
                    state["done_status"][company] = new_val
                    st.session_state["state"]["done_status"][company] = new_val
                    _save_and_push_state(
                        st.session_state["state"],
                        st.session_state.get("_gh_token",""),
                        st.session_state.get("_gh_repo",""),
                    )
                    st.rerun()

            # 의안 드롭다운 (계층 구조 렌더링)
            if agenda_open_co:
                _yr       = datetime.now().year
                _ck       = f"{company}_{_yr}"
                mem_cache = st.session_state.get("agenda_cache_mem", {})

                def _do_refresh():
                    mem_cache.pop(_ck, None)
                    disk = _load_agenda_cache()
                    disk.pop(f"{company}_{_yr}", None)
                    _save_agenda_cache(disk)
                    st.session_state["agenda_cache_mem"] = mem_cache
                    with st.spinner("재조회 중…"):
                        ni, nm = fetch_dart_agendas(company, dart_key)
                    mem_cache[_ck] = {"items": ni, "msg": nm}
                    st.session_state["agenda_cache_mem"] = mem_cache
                    st.rerun()

                if _ck in mem_cache:
                    ag_data = mem_cache[_ck]
                    items   = ag_data["items"]
                    msg     = ag_data["msg"]

                    if items:
                        def _row_html(it):
                            is_sub     = it.get("is_sub", False)
                            is_virtual = it.get("virtual", False)
                            indent_td  = "padding-left:20px;" if is_sub else ""
                            prefix     = "└ " if is_sub else ""
                            no_color   = "#818cf8" if is_sub else ("#94a3b8" if is_virtual else "#4338ca")
                            no_weight  = "500" if is_sub else "700"
                            tc         = "#dc2626" if "특별" in it.get("type","") else "#16a34a"
                            cand       = it.get("candidate","")
                            cand_html  = (f'<span style="color:#7c3aed;font-size:.75em;'
                                         f'margin-left:6px">👤{cand}</span>') if cand else ""
                            row_bg     = "#f5f3ff" if is_sub else ("" if not is_virtual else "#f8fafc")
                            return (
                                f'<tr style="border-bottom:1px solid #e0e7ff;background:{row_bg}">'
                                f'<td style="padding:3px 8px;font-size:.8em;color:{no_color};'
                                f'font-weight:{no_weight};white-space:nowrap;{indent_td}">{prefix}{it["no"]}</td>'
                                f'<td style="padding:3px 8px;font-size:.82em;color:#1f2937;{indent_td}">'
                                f'{it["title"]}{cand_html}</td>'
                                f'<td style="padding:3px 8px;font-size:.74em;color:{tc};white-space:nowrap">'
                                f'{it.get("type","") if not is_sub else ""}</td></tr>')

                        rows_html = "".join(_row_html(it) for it in items)
                        ag_c1, ag_c2 = st.columns([11, 1])
                        with ag_c1:
                            st.markdown(
                                f'<div style="background:#eef2ff;border-left:3px solid #818cf8;'
                                f'padding:6px 10px;margin:2px 0;border-radius:4px;">'
                                f'<div style="font-size:.78em;color:#4338ca;font-weight:700;margin-bottom:4px">'
                                f'📋 의안 목록 ({len(items)}건) '
                                f'<span style="font-weight:400;color:#6366f1">{msg}</span></div>'
                                f'<table style="width:100%;border-collapse:collapse">'
                                f'<tr style="background:#c7d2fe">'
                                f'<th style="padding:3px 8px;font-size:.74em;text-align:left;width:85px">번호</th>'
                                f'<th style="padding:3px 8px;font-size:.74em;text-align:left">의안명 (👤후보자)</th>'
                                f'<th style="padding:3px 8px;font-size:.74em;text-align:left;width:95px">결의유형</th></tr>'
                                f'{rows_html}</table></div>',
                                unsafe_allow_html=True)
                        with ag_c2:
                            if st.button("🔄", key=f"ag_refresh_{company}", help="다시 조회"):
                                _do_refresh()
                    else:
                        rc1, rc2 = st.columns([11, 1])
                        with rc1:
                            st.markdown(
                                f'<div style="background:#fef9c3;border-left:3px solid #fde047;'
                                f'padding:5px 10px;margin:2px 0;font-size:.82em;color:#713f12;">'
                                f'⚠️ 의안 파싱 실패 — {msg}'
                                f'<br><span style="font-size:.9em;color:#92400e">'
                                f'공시 형식 미지원 또는 미공시 상태입니다.</span></div>',
                                unsafe_allow_html=True)
                        with rc2:
                            if st.button("🔄", key=f"ag_refresh_{company}", help="다시 조회"):
                                _do_refresh()
                else:
                    st.caption("의안 조회 중…")

            # 변경 히스토리
            if hist_n and hist_open:
                for entry in reversed(hist):
                    st.markdown(
                        f'<div style="background:#f1f5f9;border-left:3px solid #94a3b8;'
                        f'padding:4px 12px;margin:1px 0;font-size:.8em;color:#475569;">'
                        f'🔁 <b>{entry["prev_name"]}</b> │ {entry["prev_date"]} │ {entry["changed_at"]}</div>',
                        unsafe_allow_html=True)

            # 수동 날짜 입력 폼
            if st.session_state.get("inline_manual") == company:
                st.markdown('<div class="manual-box">', unsafe_allow_html=True)
                st.markdown(f"📅 **{company}** 날짜 직접 입력")
                default_date = datetime(2026, 3, 25).date()
                if is_confirmed(disp):
                    try: default_date = datetime.strptime(disp, "%Y-%m-%d").date()
                    except: pass
                mc1, mc2 = st.columns([2, 3])
                with mc1:
                    picked = st.date_input("날짜", value=default_date,
                        min_value=date(2026,3,1), max_value=date(2026,3,31),
                        key=f"man_dt_{company}")
                with mc2:
                    st.markdown("<br>", unsafe_allow_html=True)
                    if st.button(f"✅ {picked.strftime('%m/%d')} 확정",
                                 type="primary", key=f"man_ok_{company}"):
                        new_d = picked.strftime("%Y-%m-%d")
                        state["overrides"][company] = new_d
                        state.setdefault("updated_recently", set()).add(company)
                        state.setdefault("updated_timestamps", {})[company] = datetime.now().isoformat()
                        pending.pop(company, None); apply_sel.discard(company)
                        st.session_state["pending_updates"] = pending
                        save_state(state)
                        st.session_state["inline_manual"] = None
                        st.rerun()
                st.markdown("</div>", unsafe_allow_html=True)

            # 기업변경 폼 (운용사 필드 포함)
            if st.session_state.get("inline_change") == company:
                st.markdown('<div class="change-box">', unsafe_allow_html=True)
                st.markdown(f"🔄 **{company}** → 다른 기업으로 교체")
                ic1, ic2, ic3 = st.columns([3, 2, 2])
                with ic1:
                    new_name = st.text_input("새 기업명", key=f"ic_nm_{company}",
                                             placeholder="예: 삼성SDI")
                with ic2:
                    new_manager_input = st.text_input("운용사", key=f"ic_mg_{company}",
                                                       value=manager,
                                                       placeholder="운용사명")
                with ic3:
                    opt = st.radio("날짜", ["직접 입력", "미정"],
                                   horizontal=True, key=f"ic_opt_{company}")
                new_date = "미정"
                if opt == "직접 입력":
                    new_date = st.date_input("날짜", value=datetime(2026,3,25).date(),
                        min_value=date(2026,3,1), max_value=date(2026,3,31),
                        key=f"ic_dt_{company}").strftime("%Y-%m-%d")
                bc1, bc2 = st.columns(2)
                with bc1:
                    if st.button("✅ 확정", type="primary",
                                 use_container_width=True, key=f"ic_ok_{company}"):
                        nn = (new_name or "").strip()
                        if nn:
                            _apply_company_change(state, company, nn, new_date, orig,
                                                  new_manager=new_manager_input.strip())
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

    # ── 진행완료 기업 분리 ──
    done_set    = {c for c, v in done_status.items() if v}
    df_done     = df[df["단체명"].isin(done_set)].reset_index(drop=True)
    df_not_done = df[~df["단체명"].isin(done_set)].reset_index(drop=True)

    df_conf_active = df_not_done[df_not_done["_conf"]].reset_index(drop=True)
    df_pend_active = df_not_done[~df_not_done["_conf"]].reset_index(drop=True)

    # ── 확정 기업 표 ──
    if not df_conf_active.empty:
        st.markdown(
            f'<div style="background:#f0fdf4;border-left:4px solid #86efac;'
            f'padding:6px 14px;margin:8px 0 4px;font-weight:700;color:#166534;">'
            f'📅 확정 기업 ({len(df_conf_active)}개)</div>', unsafe_allow_html=True)
        _render_header(prefix="conf")
        _render_rows(df_conf_active)

    # ── 미정 기업 표 ──
    if not df_pend_active.empty:
        st.markdown(
            f'<div style="background:#fff7ed;border-left:4px solid #fdba74;'
            f'padding:6px 14px;margin:16px 0 4px;font-weight:700;color:#9a3412;">'
            f'⏳ 미정 기업 ({len(df_pend_active)}개)</div>', unsafe_allow_html=True)
        _render_header(prefix="pend")
        _render_rows(df_pend_active)

    # ── 진행완료 기업 (접이식) ──
    if not df_done.empty:
        st.markdown("<br>", unsafe_allow_html=True)
        with st.expander(f"✅ 진행완료 기업 ({len(df_done)}개) — 클릭하여 펼치기",
                         expanded=False):
            st.markdown(
                '<div style="background:#f0fdf4;border-left:4px solid #4ade80;'
                'padding:4px 14px;margin:0 0 6px;font-size:.85em;color:#166534;">'
                '완료된 기업 목록입니다. ✅ 버튼을 다시 누르면 목록에서 제외됩니다.</div>',
                unsafe_allow_html=True)
            _render_header(prefix="done")
            _render_rows(df_done)

# ── 검색결과 탭 ───────────────────────────────

def render_results_tab(df: pd.DataFrame, state: dict):
    st.markdown("### 🔍 DART 검색 결과")
    st.caption("사이드바 '🔍 전체 DART 검색' 또는 리스트 개별 🔍 버튼 클릭 후 결과 표시")

    crawl     = st.session_state.get("crawl_results", {})
    overrides = state["overrides"]

    if not crawl:
        st.info("아직 검색 결과가 없습니다. DART API 키를 입력하고 검색을 실행하세요.")
        return

    rows = []
    for _, row in df.iterrows():
        corp  = row["단체명"]
        cur   = overrides.get(corp, row["주주총회일"])
        res   = crawl.get(corp, {})
        found = res.get("date")
        src   = res.get("source", "미검색")
        if found is None: continue  # 검색 안 한 항목 제외
        status = ""
        if validate_march_2026(found):
            status = "✓ 동일" if found == cur else f"→ {found}"
        rows.append({
            "기업":   corp,
            "현재":   cur,
            "검색결과": found or "—",
            "상태":   status or src[:30],
            "필수":   "🔴" if row.get("비고")=="필수단체" else "",
        })

    if not rows:
        st.info("검색된 결과가 없습니다.")
        return

    rdf = pd.DataFrame(rows)
    fc1, fc2 = st.columns(2)
    with fc1: flt = st.selectbox("필터",["전체","업데이트 가능","변경없음","조회실패"], key="rt_flt")
    with fc2: q   = st.text_input("기업명 검색", key="rt_q", placeholder="검색…")

    fdf = rdf.copy()
    if flt == "업데이트 가능": fdf = fdf[fdf["상태"].str.startswith("→", na=False)]
    elif flt == "변경없음":    fdf = fdf[fdf["상태"]=="✓ 동일"]
    elif flt == "조회실패":    fdf = fdf[~fdf["상태"].str.startswith(("✓","→"), na=False)]
    if q: fdf = fdf[fdf["기업"].str.contains(q, na=False)]

    st.caption(f"{len(fdf)} / {len(rdf)}건 (검색된 종목만)")
    st.dataframe(fdf.drop(columns=["필수"]), use_container_width=True, hide_index=True)

    applicable = fdf[fdf["상태"].str.startswith("→", na=False)]
    if not applicable.empty:
        st.markdown(f"---\n**📢 달력·리스트에 반영 가능: {len(applicable)}건**")
        if st.button(f"✅ {len(applicable)}건 일괄 반영", type="primary", key="rt_apply"):
            for _, r in applicable.iterrows():
                new_d = r["검색결과"]
                if validate_march_2026(new_d):
                    corp = r["기업"]
                    state["overrides"][corp] = new_d
                    state.setdefault("updated_recently",set()).add(corp)
                    state.setdefault("updated_timestamps",{})[corp] = datetime.now().isoformat()
            save_state(state)
            st.success("반영 완료!"); st.rerun()


# ── 메인 ─────────────────────────────────────

def main():
    init_session()

    # ── 앱 재시작 시 GitHub에서 state 자동 복원 ──
    if not os.path.exists(STATE_PATH):
        _gh_tok  = st.session_state.get("_gh_token", "")
        _gh_rep  = st.session_state.get("_gh_repo", "")
        if _gh_tok and _gh_rep:
            restored = _pull_state_from_github(_gh_tok, _gh_rep)
            if restored:
                # 복원된 state 재로드
                st.session_state["state"] = load_state()

    state    = st.session_state["state"]
    dart_key, anthropic_key = render_sidebar(state)

    # 사이드바 렌더 후 토큰이 입력되면 pull 재시도 (첫 번째 렌더 시 토큰 없을 수 있음)
    if not os.path.exists(STATE_PATH):
        _gh_tok = st.session_state.get("_gh_token", "")
        _gh_rep = st.session_state.get("_gh_repo", "")
        if _gh_tok and _gh_rep:
            if _pull_state_from_github(_gh_tok, _gh_rep):
                st.session_state["state"] = load_state()
                state = st.session_state["state"]
                st.rerun()

    st.title("📅 주주총회 일정 트래커")
    ts = max(state.get("updated_timestamps",{}).values(), default=None)
    if ts: st.caption(f"마지막 업데이트: {ts[:16]}")

    render_search_log()

    try:
        df = load_excel_data()
    except FileNotFoundError:
        st.error(f"'{EXCEL_PATH}' 파일을 app.py 와 같은 폴더에 넣어주세요.")
        return

    repl = state.get("name_replacements", {})
    if repl:
        df["단체명"] = df["단체명"].replace(repl)
        df = df.drop_duplicates(subset=["단체명"]).reset_index(drop=True)

    tab_cal, tab_list, tab_res = st.tabs(["📅 달력", "📋 리스트", "🔍 검색 결과"])

    with tab_cal:
        overrides = state["overrides"]
        conf_n = sum(1 for _,r in df.iterrows()
                     if is_confirmed(overrides.get(r["단체명"], r["주주총회일"])))
        upd_n = len(state.get("updated_recently", set()))
        m1,m2,m3,m4 = st.columns(4)
        m1.metric("전체 기업", len(df))
        m2.metric("📅 확정", conf_n, delta=f"+{upd_n} 업데이트" if upd_n else None)
        m3.metric("⏳ 미정", len(df)-conf_n)
        m4.metric("🔴 필수단체", int((df["비고"]=="필수단체").sum()))
        st.markdown("---")
        st.subheader("2026년 3월")
        st.markdown(render_calendar_html(2026, 3, build_day_map(df, state)),
                    unsafe_allow_html=True)

    with tab_list:
        render_list_view(df, state, dart_key, anthropic_key)

    with tab_res:
        render_results_tab(df, state)


if __name__ == "__main__":
    main()
