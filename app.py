import streamlit as st
import pandas as pd
import json
import os
import re
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime, date

# ─────────────────────────────────────────────
# 페이지 설정
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="주주총회 일정 트래커",
    page_icon="📋",
    layout="wide",
    initial_sidebar_state="expanded",
)

EXCEL_PATH = "주주총회.xlsx"
STATE_PATH = "agm_state.json"

# ─────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700&display=swap');

* { font-family: 'Noto Sans KR', sans-serif; }

@keyframes highlight-pulse {
    0%   { background-color: #ffe066; }
    50%  { background-color: #fff9c4; }
    100% { background-color: #ffe066; }
}

.updated-badge {
    display: inline-block;
    background-color: #ffe066;
    color: #7a5800;
    font-weight: 700;
    font-size: 0.82em;
    padding: 1px 8px;
    border-radius: 12px;
    margin-left: 6px;
    animation: highlight-pulse 1.2s ease-in-out 4;
    border: 1px solid #f5c518;
}

.date-confirmed {
    color: #1a7f37;
    font-weight: 600;
}
.date-pending {
    color: #9a6700;
    font-style: italic;
}
.required-badge {
    display: inline-block;
    background-color: #fde8e8;
    color: #c53030;
    font-size: 0.78em;
    padding: 1px 7px;
    border-radius: 10px;
    border: 1px solid #f5a5a5;
    margin-left: 4px;
}
.prev-company-block {
    background: #f1f5f9;
    border-left: 4px solid #94a3b8;
    border-radius: 0 6px 6px 0;
    padding: 8px 14px;
    margin: 4px 0 10px 0;
    font-size: 0.88em;
    color: #475569;
}
.row-card {
    background: #fff;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    padding: 8px 14px;
    margin-bottom: 6px;
    transition: box-shadow 0.2s;
}
.row-card:hover { box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
.section-header {
    font-size: 1em;
    font-weight: 700;
    color: #374151;
    padding: 6px 0 2px 0;
    border-bottom: 2px solid #e5e7eb;
    margin-bottom: 8px;
}
.crawl-new {
    background-color: #dcfce7;
    color: #166534;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 8px;
    font-size: 0.9em;
}
.crawl-same {
    color: #6b7280;
    font-size: 0.9em;
}
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────
# 데이터 로드 및 상태 관리
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
        # updated_recently → set
        raw["updated_recently"] = set(raw.get("updated_recently", []))
        return raw
    return {
        "overrides": {},       # company → new date string
        "changes": {},         # current_company → prev_company info
        "updated_recently": set(),
        "updated_timestamps": {},
    }


def save_state(state):
    out = dict(state)
    out["updated_recently"] = list(state.get("updated_recently", set()))
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


def init_session():
    if "state" not in st.session_state:
        st.session_state["state"] = load_state()
    if "change_modal" not in st.session_state:
        st.session_state["change_modal"] = None   # company being changed
    if "expanded_prev" not in st.session_state:
        st.session_state["expanded_prev"] = set()
    if "crawl_results" not in st.session_state:
        st.session_state["crawl_results"] = {}


# ─────────────────────────────────────────────
# DART 크롤링
# ─────────────────────────────────────────────

def search_dart(company_name: str) -> tuple[str | None, str]:
    """
    DART 공시 검색으로 주주총회 날짜 탐색.
    Returns (date_str | None, source_str)
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Referer": "https://dart.fss.or.kr/",
    }
    year = datetime.now().year
    try:
        url = "https://dart.fss.or.kr/dsab002/main.do"
        params = {
            "selectKey": company_name,
            "currentPage": "1",
            "maxResults": "15",
            "sort": "date",
            "series": "desc",
            "textCrpNm": company_name,
            "startDt": f"{year}0101",
            "endDt": f"{year}1231",
            "reportNamepR": "주주총회",
        }
        resp = requests.get(url, params=params, headers=headers, timeout=12)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # 공시 목록에서 날짜 파싱 시도
        rows = soup.select("table.tbList tbody tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) >= 5:
                title = cells[2].get_text(strip=True)
                rcept_dt = cells[4].get_text(strip=True)  # 접수일
                # 주주총회 소집 공고 / 주주총회 결과
                if "주주총회" in title:
                    # 날짜 형식: YYYY.MM.DD
                    m = re.search(r"(\d{4})\.(\d{2})\.(\d{2})", rcept_dt)
                    if m:
                        d = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                        return d, f"DART ({title})"
        return None, "DART: 공시 없음"
    except requests.exceptions.ConnectionError:
        return None, "네트워크 오류"
    except Exception as e:
        return None, f"오류: {e}"


def search_dart_api(company_name: str, api_key: str) -> tuple[str | None, str]:
    """
    DART OpenAPI 사용 (api_key 필요).
    Returns (date_str | None, source_str)
    """
    if not api_key:
        return None, "API 키 없음"
    year = datetime.now().year
    try:
        url = "https://opendart.fss.or.kr/api/list.json"
        params = {
            "crtfc_key": api_key,
            "corp_name": company_name,
            "bgn_de": f"{year}0101",
            "end_de": f"{year}1231",
            "pblntf_detail_ty": "G003",
            "page_count": 10,
        }
        resp = requests.get(url, params=params, timeout=12)
        data = resp.json()
        if data.get("status") == "000" and data.get("list"):
            item = data["list"][0]
            dt_raw = item.get("rcept_dt", "")
            m = re.match(r"(\d{4})(\d{2})(\d{2})", dt_raw)
            if m:
                d = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                return d, f"DART API ({item.get('report_nm','')})"
        return None, "DART API: 공시 없음"
    except Exception as e:
        return None, f"API 오류: {e}"


# ─────────────────────────────────────────────
# UI 헬퍼
# ─────────────────────────────────────────────

def is_confirmed_date(date_str: str) -> bool:
    return bool(re.match(r"\d{4}-\d{2}-\d{2}", date_str))


def render_date(company: str, date_str: str, state: dict) -> str:
    overrides = state["overrides"]
    updated_recently = state.get("updated_recently", set())

    display_date = overrides.get(company, date_str)
    badge = ""
    if company in updated_recently:
        badge = '<span class="updated-badge">🔄 업데이트됨</span>'

    if is_confirmed_date(display_date):
        return f'<span class="date-confirmed">{display_date}</span>{badge}'
    else:
        return f'<span class="date-pending">{display_date}</span>{badge}'


def apply_company_change(state: dict, old_name: str, new_name: str, original_date: str):
    """기업 변경: old_name → new_name, old_name은 '변경 1회 전 기업'으로"""
    changes = state.setdefault("changes", {})
    overrides = state.setdefault("overrides", {})

    # 이전 기업 정보 저장
    changes[new_name] = {
        "prev_name": old_name,
        "prev_date": overrides.get(old_name, original_date),
        "changed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    # 이전 기업 overrides 정리
    if old_name in overrides:
        del overrides[old_name]

    save_state(state)


# ─────────────────────────────────────────────
# 사이드바
# ─────────────────────────────────────────────

def render_sidebar(state: dict) -> str:
    st.sidebar.title("⚙️ 설정")
    dart_api_key = st.sidebar.text_input(
        "DART OpenAPI 키 (선택)",
        type="password",
        help="https://opendart.fss.or.kr 에서 발급. 없으면 웹 크롤링 사용.",
    )
    st.sidebar.markdown("---")

    # 전체 DART 검색
    if st.sidebar.button("🔍 전체 기업 DART 검색", use_container_width=True):
        df = load_excel_data()
        companies = df["단체명"].tolist()
        progress = st.sidebar.progress(0, text="검색 중...")
        results = {}
        for i, corp in enumerate(companies):
            if dart_api_key:
                found_date, source = search_dart_api(corp, dart_api_key)
            else:
                found_date, source = search_dart(corp)

            results[corp] = {"date": found_date, "source": source}
            progress.progress((i + 1) / len(companies), text=f"{corp} 검색 중…")
            time.sleep(0.3)  # rate limit

        progress.empty()
        st.session_state["crawl_results"] = results

        # 새 날짜 자동 적용
        updated = 0
        for corp, info in results.items():
            if info["date"]:
                orig_row = df[df["단체명"] == corp]
                if not orig_row.empty:
                    orig_date = orig_row.iloc[0]["주주총회일"]
                    override_date = state["overrides"].get(corp, orig_date)
                    if info["date"] != override_date:
                        state["overrides"][corp] = info["date"]
                        state.setdefault("updated_recently", set()).add(corp)
                        state.setdefault("updated_timestamps", {})[corp] = datetime.now().isoformat()
                        updated += 1
        save_state(state)
        if updated:
            st.sidebar.success(f"✅ {updated}개 기업 날짜 업데이트됨")
        else:
            st.sidebar.info("변경된 날짜 없음")
        st.rerun()

    st.sidebar.markdown("---")
    # 업데이트 기록 초기화
    if st.sidebar.button("🗑️ 업데이트 표시 초기화", use_container_width=True):
        state["updated_recently"] = set()
        save_state(state)
        st.rerun()

    # 전체 상태 초기화
    if st.sidebar.button("⚠️ 전체 상태 초기화", use_container_width=True, type="secondary"):
        if os.path.exists(STATE_PATH):
            os.remove(STATE_PATH)
        st.session_state["state"] = load_state()
        st.rerun()

    return dart_api_key


# ─────────────────────────────────────────────
# 메인 테이블 렌더링
# ─────────────────────────────────────────────

def render_table(df: pd.DataFrame, state: dict, dart_api_key: str):
    overrides = state["overrides"]
    changes = state.get("changes", {})
    updated_recently = state.get("updated_recently", set())
    crawl_results = st.session_state.get("crawl_results", {})

    # 날짜별로 그룹화
    def get_display_date(row):
        return overrides.get(row["단체명"], row["주주총회일"])

    df["_display_date"] = df.apply(get_display_date, axis=1)
    df["_is_confirmed"] = df["_display_date"].apply(is_confirmed_date)

    confirmed = df[df["_is_confirmed"]].sort_values("_display_date")
    pending = df[~df["_is_confirmed"]]

    for section_label, section_df in [("📅 주주총회 일자 확정", confirmed), ("⏳ 미정", pending)]:
        if section_df.empty:
            continue
        st.markdown(f'<div class="section-header">{section_label}</div>', unsafe_allow_html=True)

        # 날짜별 그룹
        if section_label.startswith("📅"):
            groups = section_df.groupby("_display_date", sort=False)
            group_items = [(dt, grp) for dt, grp in sorted(groups, key=lambda x: x[0])]
        else:
            group_items = [("미정", section_df)]

        for group_date, group_df in group_items:
            if section_label.startswith("📅"):
                st.markdown(
                    f"**🗓 {group_date}** <span style='color:#9ca3af;font-size:0.85em'>({len(group_df)}개 기업)</span>",
                    unsafe_allow_html=True
                )

            for _, row in group_df.iterrows():
                company = row["단체명"]
                orig_date = row["주주총회일"]
                display_date = overrides.get(company, orig_date)
                is_required = row["비고"] == "필수단체"
                is_updated = company in updated_recently
                has_prev = company in changes

                # ── 행 컨테이너 ──
                with st.container():
                    col_name, col_date, col_dart, col_change = st.columns([3, 2.5, 1.5, 1.5])

                    # 기업명
                    with col_name:
                        name_html = company
                        if is_required:
                            name_html += ' <span class="required-badge">필수단체</span>'
                        if has_prev:
                            prev_key = f"expand_{company}"
                            is_expanded = company in st.session_state["expanded_prev"]
                            arrow = "▼" if is_expanded else "▶"
                            if st.button(
                                f"{arrow} {company}" + (" ★" if is_required else ""),
                                key=f"btn_expand_{company}",
                                help="이전 기업 보기",
                            ):
                                if company in st.session_state["expanded_prev"]:
                                    st.session_state["expanded_prev"].discard(company)
                                else:
                                    st.session_state["expanded_prev"].add(company)
                                st.rerun()
                        else:
                            st.markdown(name_html, unsafe_allow_html=True)

                    # 날짜
                    with col_date:
                        date_html = render_date(company, orig_date, state)
                        st.markdown(date_html, unsafe_allow_html=True)

                    # DART 검색 (개별)
                    with col_dart:
                        if st.button("🔍 DART", key=f"dart_{company}", help="DART에서 날짜 검색"):
                            with st.spinner(f"{company} 검색 중…"):
                                if dart_api_key:
                                    found, src = search_dart_api(company, dart_api_key)
                                else:
                                    found, src = search_dart(company)
                                crawl_results[company] = {"date": found, "source": src}
                                st.session_state["crawl_results"] = crawl_results

                                if found and found != display_date:
                                    state["overrides"][company] = found
                                    state.setdefault("updated_recently", set()).add(company)
                                    state.setdefault("updated_timestamps", {})[company] = datetime.now().isoformat()
                                    save_state(state)
                                    st.rerun()

                        # 검색 결과 표시
                        if company in crawl_results:
                            res = crawl_results[company]
                            if res["date"] and res["date"] != display_date:
                                st.markdown(f'<span class="crawl-new">→ {res["date"]}</span>', unsafe_allow_html=True)
                            elif res["date"]:
                                st.markdown(f'<span class="crawl-same">✓ 동일</span>', unsafe_allow_html=True)
                            else:
                                st.caption(res["source"])

                    # 기업 변경 버튼
                    with col_change:
                        if st.button("✏️ 기업변경", key=f"change_{company}", help="기업명 변경"):
                            st.session_state["change_modal"] = {
                                "old_name": company,
                                "orig_date": orig_date,
                            }
                            st.rerun()

                # 이전 기업 펼침
                if has_prev and company in st.session_state["expanded_prev"]:
                    prev = changes[company]
                    st.markdown(
                        f"""<div class="prev-company-block">
                        🔁 <strong>변경 1회 전 기업</strong>: {prev['prev_name']}
                        &nbsp;|&nbsp; 날짜: {prev['prev_date']}
                        &nbsp;|&nbsp; 변경일시: {prev['changed_at']}
                        </div>""",
                        unsafe_allow_html=True,
                    )

        st.markdown("<br>", unsafe_allow_html=True)


# ─────────────────────────────────────────────
# 기업 변경 모달
# ─────────────────────────────────────────────

def render_change_modal(df: pd.DataFrame, state: dict):
    modal_info = st.session_state.get("change_modal")
    if not modal_info:
        return

    old_name = modal_info["old_name"]
    orig_date = modal_info["orig_date"]

    with st.expander(f"✏️ 기업 변경: {old_name}", expanded=True):
        st.info(
            f"**{old_name}** 를 다른 기업으로 변경합니다.\n\n"
            "기존 기업은 **변경 1회 전 기업**으로 분류되어 기업명 클릭 시 확인할 수 있습니다."
        )
        new_name = st.text_input(
            "새 기업명을 입력하세요",
            placeholder="예: 삼성SDI",
            key="new_company_input",
        )

        # 새 기업의 날짜 선택
        new_date_option = st.radio(
            "새 기업의 주주총회 날짜",
            ["직접 입력", "미정으로 설정"],
            horizontal=True,
            key="new_date_option",
        )
        new_date_str = ""
        if new_date_option == "직접 입력":
            new_date_input = st.date_input("날짜 선택", key="new_date_input")
            new_date_str = new_date_input.strftime("%Y-%m-%d")
        else:
            new_date_str = "미정"

        col_ok, col_cancel = st.columns(2)
        with col_ok:
            if st.button("✅ 변경 확정", type="primary", use_container_width=True):
                if new_name.strip():
                    # df에 새 기업 행 추가 (캐시 무효화)
                    apply_company_change(state, old_name, new_name.strip(), orig_date)
                    # 새 기업의 날짜 override 설정
                    state["overrides"][new_name.strip()] = new_date_str
                    # 원래 기업 행을 새 기업으로 교체하기 위해 별도 매핑 저장
                    state.setdefault("name_replacements", {})[old_name] = new_name.strip()
                    save_state(state)
                    load_excel_data.clear()
                    st.session_state["change_modal"] = None
                    st.success(f"✅ {old_name} → {new_name.strip()} 로 변경되었습니다.")
                    st.rerun()
                else:
                    st.error("기업명을 입력해주세요.")

        with col_cancel:
            if st.button("❌ 취소", use_container_width=True):
                st.session_state["change_modal"] = None
                st.rerun()


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────

def main():
    init_session()
    state = st.session_state["state"]

    # 사이드바
    dart_api_key = render_sidebar(state)

    # 헤더
    st.title("📋 주주총회 일정 트래커")
    last_updated = max(state.get("updated_timestamps", {}).values(), default=None)
    if last_updated:
        st.caption(f"마지막 업데이트: {last_updated[:16]}")

    # 모달 먼저 (위에 표시)
    render_change_modal(None, state)

    # 엑셀 로드
    try:
        df = load_excel_data()
    except FileNotFoundError:
        st.error(f"'{EXCEL_PATH}' 파일을 찾을 수 없습니다. 앱과 같은 폴더에 파일을 놓아주세요.")
        return

    # 기업명 교체 적용
    name_replacements = state.get("name_replacements", {})
    if name_replacements:
        df["단체명"] = df["단체명"].replace(name_replacements)
        # 이미 존재하는 기업명이면 중복 제거
        df = df.drop_duplicates(subset=["단체명"]).reset_index(drop=True)

    # 헤더 행
    h1, h2, h3, h4 = st.columns([3, 2.5, 1.5, 1.5])
    h1.markdown("**기업명**")
    h2.markdown("**주주총회일**")
    h3.markdown("**DART 검색**")
    h4.markdown("**기업 변경**")
    st.divider()

    # 테이블 렌더링
    render_table(df, state, dart_api_key)

    # 범례
    with st.expander("ℹ️ 범례 / 사용법"):
        st.markdown("""
        | 색상/표시 | 의미 |
        |---|---|
        | 🟢 초록색 날짜 | 주주총회 일자 확정 |
        | 🟡 이탤릭 날짜 | 미정 (예상일) |
        | 🟡 `업데이트됨` 배지 | DART 검색으로 날짜가 변경된 기업 |
        | 🔴 `필수단체` 배지 | 필수 의결권 행사 대상 |
        | ▶ 기업명 버튼 | 이전 기업 정보 펼치기/닫기 |

        **DART 검색 방법:**
        - 개별: 각 기업의 🔍 DART 버튼 클릭
        - 전체: 사이드바 > 🔍 전체 기업 DART 검색
        - DART OpenAPI 키 입력 시 더 정확한 결과 제공

        **기업 변경:**
        - ✏️ 기업변경 버튼 → 새 기업명 입력 → 확정
        - 기존 기업은 새 기업명 클릭 시 '변경 1회 전 기업'으로 확인 가능
        """)


if __name__ == "__main__":
    main()
