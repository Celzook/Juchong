import streamlit as st
import pandas as pd
import json, os, re, time, calendar, zipfile, io
import xml.etree.ElementTree as ET
import requests
from datetime import datetime, date

st.set_page_config(page_title="주주총회 일정 트래커", page_icon="📅",
                   layout="wide", initial_sidebar_state="expanded")

EXCEL_PATH = "주주총회.xlsx"
STATE_PATH = "agm_state.json"
CORP_CACHE = "dart_corp_codes.json"

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
.chip{display:inline-block;border-radius:10px;padding:2px 7px;margin:2px 2px 0 0;font-size:.73em;font-weight:500;line-height:1.6;cursor:default;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.chip-confirmed{background:#dcfce7;color:#166534;border:1px solid #86efac;}
.chip-confirmed.req{background:#dbeafe;color:#1e40af;border:1px solid #93c5fd;}
.chip-updated{background:#fef9c3;color:#854d0e;border:1.5px solid #fde047;}
.chip-pending{background:#fff7ed;color:#9a3412;border:1px dashed #fdba74;font-style:italic;}
.chip-pending.req{background:#fef3c7;color:#92400e;border:1px dashed #fcd34d;}
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
    df = pd.read_excel(EXCEL_PATH, sheet_name="리스트 대상 운용사", usecols="B:D", header=1)
    df.columns = ["단체명", "주주총회일", "비고"]
    df = df.dropna(subset=["단체명"])
    df["단체명"]    = df["단체명"].astype(str).str.strip()
    df["주주총회일"] = df["주주총회일"].apply(
        lambda x: x.strftime("%Y-%m-%d") if hasattr(x,"strftime") else str(x).strip())
    df["비고"] = df["비고"].fillna("").astype(str).str.strip()
    return df.reset_index(drop=True)


def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        try:
            raw = json.load(open(STATE_PATH, encoding="utf-8"))
            raw["updated_recently"] = set(raw.get("updated_recently", []))
            return raw
        except Exception:
            pass
    return {"overrides":{}, "changes":{}, "updated_recently":set(),
            "updated_timestamps":{}, "name_replacements":{}}


def save_state(state: dict):
    out = dict(state)
    out["updated_recently"] = list(state.get("updated_recently", set()))
    json.dump(out, open(STATE_PATH,"w",encoding="utf-8"), ensure_ascii=False, indent=2)


def init_session():
    for k, v in [
        ("state",         load_state()),
        ("inline_change", None),   # 기업변경 인라인: company name
        ("inline_manual", None),   # 수동날짜입력 인라인: company name
        ("expanded_prev", set()),
        ("crawl_results", {}),
        ("search_log",    None),
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


def search_dart_api(company_name: str, api_key: str):
    """DART API로 주총 확정일 조회. Returns (날짜|None, 메시지)"""
    if not api_key:
        return None, "API 키 없음"
    try:
        corp_dict = load_corp_codes(api_key)
        corp_code = find_corp_code(corp_dict, company_name)
        if not corp_code:
            return None, "기업코드 미발견"

        year = datetime.now().year
        r = requests.get(
            "https://opendart.fss.or.kr/api/list.json",
            params={"crtfc_key": api_key, "corp_code": corp_code,
                    "bgn_de": f"{year}0101", "end_de": f"{year}0331",
                    "last_report_at": "N", "page_no": "1", "page_count": "100"},
            timeout=12)
        data = r.json()
        if data.get("status") != "000":
            return None, f"DART: {data.get('message','오류')}"

        rcept_no = report_nm = ""
        for kw in ["주주총회소집결의", "주주총회소집공고", "주주총회", "정기총회"]:
            for item in data.get("list", []):
                if kw in item.get("report_nm",""):
                    rcept_no  = item["rcept_no"]
                    report_nm = item["report_nm"]
                    break
            if rcept_no: break

        if not rcept_no:
            return None, "공시 없음"

        # 공시 본문 XML에서 날짜 파싱
        rz = requests.get(
            "https://opendart.fss.or.kr/api/document.xml",
            params={"crtfc_key": api_key, "rcept_no": rcept_no.replace("-","")},
            timeout=20)
        rz.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(rz.content))
        xml_names = [f for f in zf.namelist() if f.lower().endswith(".xml")]
        if not xml_names:
            return None, "XML 없음"
        raw = zf.read(xml_names[0]).decode("utf-8", errors="ignore")

        # 날짜 파싱 패턴 (광범위)
        patterns = [
            r"(?:주주총회|정기총회|소집일|개최일)[^0-9]{0,30}?(\d{4})[년.\s\-]*(\d{1,2})[월.\s\-]*(\d{1,2})",
            r"(\d{4})[.\-년\s]+(\d{1,2})[.\-월\s]+(\d{1,2})[^0-9]{0,30}(?:주주총회|소집|개최)",
            r"\b(2026)[.\-](0?[1-9]|1[0-2])[.\-](0?[1-9]|[12]\d|3[01])\b",
        ]
        for pat in patterns:
            for m in re.finditer(pat, raw, re.IGNORECASE):
                d = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                if validate_march_2026(d):
                    return d, f"DART ({report_nm[:20]})"

        return None, f"날짜 파싱 실패 ({report_nm[:20]})"

    except requests.exceptions.ConnectionError:
        return None, "네트워크 오류"
    except Exception as e:
        return None, f"오류: {str(e)[:40]}"


# ── Claude AI 웹검색 ──────────────────────────

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
    day_map = {}
    overrides = state["overrides"]
    updated   = state.get("updated_recently", set())
    for _, row in df.iterrows():
        company = row["단체명"]
        disp    = overrides.get(company, row["주주총회일"])
        conf    = is_confirmed(disp)
        key     = disp[:10] if conf else extract_pending_date(row["주주총회일"])
        if not key: continue
        day_map.setdefault(key, []).append({
            "name": company, "confirmed": conf,
            "required": row["비고"] == "필수단체",
            "updated": company in updated,
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
                if it["updated"]:                     cls = "chip chip-updated"
                elif it["confirmed"] and it["required"]: cls = "chip chip-confirmed req"
                elif it["confirmed"]:                 cls = "chip chip-confirmed"
                elif it["required"]:                  cls = "chip chip-pending req"
                else:                                 cls = "chip chip-pending"
                pfx = "★" if it["required"] else ""
                sfx = "" if it["confirmed"] else " *"
                cell += f'<span class="{cls}">{pfx}{it["name"]}{sfx}</span>'
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
    """공통 전체검색 루프. search_fn(corp) -> (date|None, source)"""
    corps    = df["단체명"].tolist()
    total    = len(corps)
    prog     = st.sidebar.progress(0)
    results  = st.session_state.get("crawl_results", {})
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
                state["overrides"][corp] = found
                state.setdefault("updated_recently", set()).add(corp)
                state.setdefault("updated_timestamps", {})[corp] = datetime.now().isoformat()
                upd_list.append((corp, cur, found, src))
            else:
                unc_list.append((corp, found))
        else:
            nor_list.append((corp, src))
        time.sleep(delay)

    prog.empty()
    st.session_state["crawl_results"] = results
    st.session_state["search_log"] = {
        "label":     label,
        "updated":   upd_list,
        "unchanged": unc_list,
        "no_result": nor_list,
        "ran_at":    datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    save_state(state)
    st.rerun()


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
    if st.sidebar.button("🗑️ 업데이트 표시 초기화", use_container_width=True):
        state["updated_recently"] = set(); save_state(state); st.rerun()
    if st.sidebar.button("⚠️ 전체 초기화", use_container_width=True, type="secondary"):
        for p in [STATE_PATH, CORP_CACHE]:
            if os.path.exists(p): os.remove(p)
        st.session_state["state"] = load_state(); st.rerun()

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

def render_list_view(df: pd.DataFrame, state: dict, dart_key: str, anthropic_key: str = ""):
    overrides        = state["overrides"]
    changes          = state.get("changes", {})
    updated_recently = state.get("updated_recently", set())
    crawl            = st.session_state.get("crawl_results", {})

    df = df.copy()
    df["_disp"] = df.apply(lambda r: overrides.get(r["단체명"], r["주주총회일"]), axis=1)
    df["_conf"] = df["_disp"].apply(is_confirmed)

    for label, sub_df in [
        ("📅 확정", df[df["_conf"]].sort_values("_disp")),
        ("⏳ 미정", df[~df["_conf"]]),
    ]:
        if sub_df.empty: continue
        st.markdown(
            f"### {label} <span style='font-size:.75em;color:#6b7280'>({len(sub_df)}개)</span>",
            unsafe_allow_html=True)

        for _, row in sub_df.iterrows():
            company  = row["단체명"]
            orig     = row["주주총회일"]
            disp     = overrides.get(company, orig)
            required = row["비고"] == "필수단체"
            updated  = company in updated_recently
            has_prev = company in changes

            c1, c2, c3, c4, c5 = st.columns([3, 2.5, 1.5, 1.8, 1.8])

            with c1:
                req_sfx  = " 🔴" if required else ""
                upd_html = ' <span class="updated-badge">🔄 업데이트</span>' if updated else ""
                if has_prev:
                    exp = company in st.session_state["expanded_prev"]
                    if st.button(f"{'▼' if exp else '▶'} {company}{req_sfx}",
                                 key=f"exp_{company}"):
                        if exp: st.session_state["expanded_prev"].discard(company)
                        else:   st.session_state["expanded_prev"].add(company)
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
                # 개별 검색 (AI 우선, DART fallback)
                can_search = anthropic_key or dart_key
                if can_search:
                    tip = "AI 웹검색" if anthropic_key else "DART API 검색"
                    if st.button("🔍", key=f"srch_{company}", help=tip):
                        with st.spinner(f"{company}…"):
                            if anthropic_key:
                                found, src = search_via_claude(company, anthropic_key)
                            else:
                                found, src = search_dart_api(company, dart_key)
                            crawl[company] = {"date": found, "source": src}
                            st.session_state["crawl_results"] = crawl
                            if validate_march_2026(found) and found != disp:
                                state["overrides"][company] = found
                                state.setdefault("updated_recently",set()).add(company)
                                state.setdefault("updated_timestamps",{})[company] = datetime.now().isoformat()
                                save_state(state)
                                st.rerun()
                    if company in crawl:
                        fd = crawl[company].get("date")
                        if validate_march_2026(fd) and fd != disp:
                            st.markdown(f'<span class="src-ok">→ {fd}</span>', unsafe_allow_html=True)
                        elif validate_march_2026(fd):
                            st.markdown('<span class="src-same">✓</span>', unsafe_allow_html=True)
                        else:
                            st.markdown(f'<span class="src-err">✗</span>', unsafe_allow_html=True,
                                        help=crawl[company].get("source",""))

            with c4:
                # 수동 날짜 입력 토글
                manual_active = st.session_state.get("inline_manual") == company
                if st.button("✖ 닫기" if manual_active else "✏️ 날짜입력",
                             key=f"man_{company}"):
                    # 다른 인라인 닫기
                    st.session_state["inline_manual"] = None if manual_active else company
                    st.session_state["inline_change"]  = None
                    st.rerun()

            with c5:
                # 기업변경 토글
                change_active = st.session_state.get("inline_change") == company
                if st.button("✖ 취소" if change_active else "🔄 기업변경",
                             key=f"chg_{company}"):
                    st.session_state["inline_change"]  = None if change_active else company
                    st.session_state["inline_manual"]  = None
                    st.rerun()

            # 이전 기업 정보
            if has_prev and company in st.session_state["expanded_prev"]:
                prev = changes[company]
                st.markdown(
                    f'<div style="background:#f1f5f9;border-left:4px solid #94a3b8;'
                    f'padding:7px 14px;margin:3px 0 4px;font-size:.85em;color:#475569;">'
                    f'🔁 <b>변경 전</b>: {prev["prev_name"]} | '
                    f'날짜: {prev["prev_date"]} | {prev["changed_at"]}</div>',
                    unsafe_allow_html=True)

            # ── 수동 날짜 입력 폼 ──
            if st.session_state.get("inline_manual") == company:
                st.markdown('<div class="manual-box">', unsafe_allow_html=True)
                st.markdown(f"📅 **{company}** 주총 날짜 직접 입력")
                st.caption("DART 사이트(dart.fss.or.kr)에서 확인한 날짜를 입력하세요.")
                default_date = datetime(2026, 3, 25).date()
                if is_confirmed(disp):
                    try: default_date = datetime.strptime(disp, "%Y-%m-%d").date()
                    except: pass
                mc1, mc2 = st.columns([2, 3])
                with mc1:
                    picked = st.date_input(
                        "날짜 선택",
                        value=default_date,
                        min_value=date(2026, 3, 1),
                        max_value=date(2026, 3, 31),
                        key=f"man_dt_{company}",
                    )
                with mc2:
                    st.markdown("<br>", unsafe_allow_html=True)
                    if st.button(f"✅ {picked.strftime('%m/%d')} 확정",
                                 type="primary", key=f"man_ok_{company}"):
                        new_d = picked.strftime("%Y-%m-%d")
                        state["overrides"][company] = new_d
                        state.setdefault("updated_recently",set()).add(company)
                        state.setdefault("updated_timestamps",{})[company] = datetime.now().isoformat()
                        save_state(state)
                        st.session_state["inline_manual"] = None
                        st.rerun()
                st.markdown("</div>", unsafe_allow_html=True)

            # ── 기업변경 폼 ──
            if st.session_state.get("inline_change") == company:
                st.markdown('<div class="change-box">', unsafe_allow_html=True)
                st.markdown(f"🔄 **{company}** → 다른 기업으로 교체  "
                            f"_(기존 기업은 '변경 전'으로 기록)_")
                ic1, ic2 = st.columns([3, 2])
                with ic1:
                    new_name = st.text_input("새 기업명", key=f"ic_nm_{company}",
                                             placeholder="예: 삼성SDI")
                with ic2:
                    opt = st.radio("날짜", ["직접 입력","미정"],
                                   horizontal=True, key=f"ic_opt_{company}")
                new_date = "미정"
                if opt == "직접 입력":
                    new_date = st.date_input(
                        "날짜", value=datetime(2026,3,25).date(),
                        min_value=date(2026,3,1), max_value=date(2026,3,31),
                        key=f"ic_dt_{company}"
                    ).strftime("%Y-%m-%d")
                bc1, bc2 = st.columns(2)
                with bc1:
                    if st.button("✅ 확정", type="primary",
                                 use_container_width=True, key=f"ic_ok_{company}"):
                        nn = (new_name or "").strip()
                        if nn:
                            state.setdefault("changes",{})[nn] = {
                                "prev_name": company,
                                "prev_date": state["overrides"].get(company, orig),
                                "changed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                            }
                            state["overrides"].pop(company, None)
                            state["overrides"][nn] = new_date
                            state.setdefault("name_replacements",{})[company] = nn
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
    state    = st.session_state["state"]
    dart_key, anthropic_key = render_sidebar(state)

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
