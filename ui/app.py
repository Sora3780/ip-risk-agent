"""IP DeteDog — 웹 화면.

두 가지 입력 방식을 제공한다.
  폴더 지정  : 로컬 실행용. 네이티브 폴더 선택 창을 띄운다. 압축 불필요.
  파일 업로드: 배포 환경용. 감시기가 없는 사람도 배포 URL 만으로 써볼 수 있어야 한다.

실행:
  streamlit run ui/app.py
"""

from __future__ import annotations

import io
import json
import os
import zipfile

import requests
import streamlit as st

API = os.environ.get("IPAGENT_API", "http://127.0.0.1:8000").rstrip("/")

WATCH_NAMES = {
    "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg",
    "package.json", "README.md", "LICENSE", "LICENSE.md", "NOTICE",
}
CODE_EXT = {".py", ".js", ".ts", ".java", ".go", ".c", ".h", ".cpp", ".rs"}
# 기획서·설계서. 특허 검토 대상이라 ZIP 업로드에서도 빠지면 안 된다.
DOC_EXT = {".md", ".txt", ".rst"}
SKIP_PARTS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".idea", "__MACOSX"}

TIER_ICON = {"FORBIDDEN": "🔴", "RESTRICTED": "🟡", "REVIEW": "⚪"}

st.set_page_config(page_title="IP DeteDog", page_icon="🔍", layout="wide")


# ------------------------------------------------------------------ 유틸

def is_target(path: str) -> bool:
    parts = path.split("/")
    if any(p in SKIP_PARTS or p.startswith(".") for p in parts[:-1]):
        return False
    name = parts[-1]
    if not name or name.startswith("."):
        return False
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return name in WATCH_NAMES or ext in CODE_EXT or ext in DOC_EXT


def strip_common_root(paths: list[str]) -> dict[str, str]:
    """폴더를 압축하면 최상위 디렉터리가 하나 끼는 경우가 많다. 그걸 벗겨낸다."""
    roots = {p.split("/", 1)[0] for p in paths if "/" in p}
    if len(roots) == 1 and all(p.startswith(next(iter(roots)) + "/") for p in paths if "/" in p):
        prefix = next(iter(roots)) + "/"
        return {p: p[len(prefix):] for p in paths}
    return {p: p for p in paths}


def changes_from_zip(raw: bytes) -> list[dict]:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        mapping = strip_common_root(names)
        out = []
        for original in names:
            rel = mapping[original]
            if not rel or not is_target(rel) or zf.getinfo(original).file_size > 1_000_000:
                continue
            out.append({
                "path": rel, "change_type": "created",
                "content": zf.read(original).decode("utf-8", errors="ignore"),
            })
    return out


def pick_folder() -> str | None:
    """네이티브 폴더 선택 창. Streamlit 이 사용자 PC 에서 돌 때만 의미가 있다."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(title="검사할 프로젝트 폴더 선택")
        root.destroy()
        return chosen or None
    except Exception:
        return None


def render_finding(f: dict) -> None:
    st.markdown(f"#### {TIER_ICON.get(f['tier'], '⚪')} `{f['locator']}`")
    c1, c2 = st.columns([1, 3])
    c1.metric("등급", f["tier"])
    c2.markdown(f"**라이선스** `{f['license'] or '미상'}`  \n{f['why']}")

    llm = f.get("llm")
    if llm:
        if llm.get("explanation_ko"):
            st.info(llm["explanation_ko"])
        cols = st.columns(2)
        if llm.get("obligations_ko"):
            cols[0].markdown("**의무사항**\n" + "\n".join(f"- {o}" for o in llm["obligations_ko"]))
        if llm.get("actions_ko"):
            cols[1].markdown("**권장 조치**\n" + "\n".join(f"- {a}" for a in llm["actions_ko"]))
        flags = []
        if llm.get("needs_legal_review"):
            flags.append("⚖️ 전문가 검토 필요")
        flags.append("✅ 근거 기반" if llm.get("grounded") else "⚠️ 근거 부족 — 참고만 할 것")
        st.caption(" · ".join(flags) + f" · 판정 {llm.get('verdict', '')}")

    if f.get("evidence_ko"):
        with st.expander("근거 원문 보기"):
            st.text(f["evidence_ko"])
            st.caption(f"출처: {f.get('evidence_source', '')}")
    else:
        st.caption("근거 문서 없음 — 라이선스를 특정할 수 없어 검색하지 않았습니다")
    st.divider()


def render_result(result: dict) -> None:
    findings = result["findings"]
    tiers = result["stats"]["by_tier"]
    m = st.columns(4)
    m[0].metric("탐지", f"{len(findings)}건")
    m[1].metric("🔴 위험", tiers.get("FORBIDDEN", 0))
    m[2].metric("🟡 주의", tiers.get("RESTRICTED", 0))
    m[3].metric("⚪ 확인필요", tiers.get("REVIEW", 0))
    st.caption(f"소요 {result['stats']['elapsed_sec']}초"
               + (f" · 대상 {result['source_path']}" if result.get("source_path") else ""))

    # 무엇을 검사했는지 먼저 밝힌다. 이게 없으면 "0건 = 안전"으로 오독된다.
    cov = result.get("coverage") or {}
    if cov:
        manifests = ", ".join(cov.get("manifests", [])) or "없음"
        st.caption(
            f"검사 범위 — 의존성 파일: {manifests} · 해석된 의존성 {cov.get('dependencies_resolved', 0)}개 "
            f"· 소스 파일 {cov.get('code_files', 0)}개 · 라이선스 표기 {cov.get('declarations', 0)}개"
        )

    if not findings:
        if not cov.get("scannable", True):
            st.error(
                "**검사할 대상이 없습니다.** 위험이 없다는 뜻이 아닙니다.\n\n"
                + "\n".join(f"- {n}" for n in cov.get("notes", [])),
                icon="🚫",
            )
            st.info("기획서만 있는 폴더라면 **특허 검토** 탭을 이용하세요.", icon="💡")
        elif cov.get("notes"):
            st.warning(
                "탐지된 리스크는 없으나 **일부만 검사했습니다.**\n\n"
                + "\n".join(f"- {n}" for n in cov["notes"]),
                icon="⚠️",
            )
        else:
            st.success(
                f"탐지된 리스크가 없습니다. 의존성 {cov.get('dependencies_resolved', 0)}개와 "
                f"소스 {cov.get('code_files', 0)}개를 검사했습니다."
            )
        return

    if cov.get("notes"):
        st.warning("검사 범위 제한: " + " / ".join(cov["notes"]), icon="⚠️")
    st.divider()
    for f in findings:
        render_finding(f)
    st.download_button(
        "리포트 JSON 내려받기",
        json.dumps(result, ensure_ascii=False, indent=2),
        file_name=f"ip-risk-{result.get('workspace_id', 'report')}.json",
        mime="application/json",
    )


def run_scan(endpoint: str, payload: dict) -> dict | None:
    with st.spinner("검사 중… 심층 검토가 켜져 있으면 시간이 걸립니다"):
        try:
            resp = requests.post(f"{API}{endpoint}", json=payload, timeout=600)
            if resp.status_code == 403:
                st.error(
                    "폴더 경로 검사가 꺼져 있습니다. 서버를 이렇게 다시 띄우세요:\n\n"
                    "```\nset IPAGENT_ALLOW_LOCAL_PATH=1\n"
                    "uvicorn ipagent.api:app --port 8000\n```"
                )
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            st.error(f"검사 실패: {exc}")
            return None


# ------------------------------------------------------------------ 화면

st.title("🔍 IP DeteDog")
st.caption("프로젝트 산출물의 변경을 공개 IP 데이터와 대조해 라이선스 리스크를 조기 탐지합니다")

health: dict = {}
with st.sidebar:
    st.subheader("서버")
    try:
        health = requests.get(f"{API}/health", timeout=10).json()
        st.success("연결됨")
        st.caption(f"라이선스 {health['policy_licenses']}종 · RAG {'ON' if health['rag_index'] else 'OFF'}")
        st.caption(f"기준: {health['baseline_license']} (비공개 상용 배포)")
    except Exception as exc:
        st.error(f"서버 연결 실패\n\n{API}")
        st.caption(str(exc)[:200])
    st.divider()
    use_llm = st.toggle("Gemini 심층 검토", value=True, help="근거 검색 + 설명 생성. 건당 약 6초")
    st.caption("끄면 규칙 판정만 수행합니다 (즉시)")

tab_scan, tab_patent, tab_timeline = st.tabs(["라이선스 검사", "특허 검토", "변경 이력"])

with tab_scan:
    # 처음 온 사람은 올릴 프로젝트가 없다. 아무 준비 없이 결과를 볼 경로를 맨 앞에 둔다.
    if health.get("demo_available"):
        c_demo, c_txt = st.columns([1, 2])
        if c_demo.button("🎬 샘플로 바로 체험", use_container_width=True, key="demo"):
            result = run_scan("/api/demo-scan", {"llm": use_llm})
            if result:
                st.session_state["result_demo"] = result
                st.session_state.pop("result_path", None)
                st.session_state.pop("result_up", None)
        c_txt.caption(
            "올릴 프로젝트가 없어도 됩니다. GPL 의존성과 문서 모순을 심어둔 "
            "데모 프로젝트를 바로 검사합니다."
        )
        if st.session_state.get("result_demo"):
            render_result(st.session_state["result_demo"])
            st.divider()

    # 폴더 지정은 화면과 서버가 같은 PC 에 있을 때만 의미가 있다.
    # 배포 환경에서 이걸 기본값으로 띄우면 누구도 쓸 수 없는 입력창이 첫 화면이 된다.
    if health.get("local_path_enabled"):
        mode = st.radio(
            "검사 방식",
            ["📁 폴더 지정", "📦 파일 업로드"],
            horizontal=True,
            captions=["로컬 실행 — 압축 불필요", "ZIP 또는 개별 파일"],
        )
    else:
        mode = "📦 파일 업로드"
        st.caption("직접 검사하려면 아래에 프로젝트를 올리세요.")
    st.divider()

    # ---------------------------------------------------------- 폴더 지정
    if mode.startswith("📁"):
        c1, c2 = st.columns([3, 1])
        if c2.button("폴더 선택…", use_container_width=True):
            picked = pick_folder()
            if picked:
                st.session_state["folder"] = picked
            else:
                st.warning("폴더 선택 창을 열 수 없습니다. 경로를 직접 붙여넣으세요.")
        folder = c1.text_input(
            "폴더 경로", value=st.session_state.get("folder", ""),
            placeholder=r"C:\Users\...\my-project",
        )
        ws = st.text_input("워크스페이스 ID", value="local-folder", key="ws_path")

        if st.button("검사 실행", type="primary", use_container_width=True,
                     disabled=not folder, key="run_path"):
            result = run_scan("/api/scan-path", {
                "workspace_id": ws, "path": folder, "llm": use_llm, "doc_check": True,
            })
            if result:
                st.session_state["result_path"] = result

        if st.session_state.get("result_path"):
            render_result(st.session_state["result_path"])

    # -------------------------------------------------------- 파일 업로드
    else:
        st.markdown(
            "**무엇을 올리면 되나요**  \n"
            "- 프로젝트 폴더를 **ZIP으로 압축**해서 올리면 가장 정확합니다  \n"
            "- 최소한 `requirements.txt` · `pyproject.toml` · `package.json` 중 하나면 의존성 검사가 됩니다  \n"
            "- **`README.md`를 함께 올리면** 문서에 적힌 라이선스와 실제 코드의 모순까지 잡습니다"
        )
        col_a, col_b = st.columns([2, 1])
        uploaded = col_a.file_uploader(
            "파일 선택", type=["zip", "txt", "toml", "md", "py", "js", "ts", "json"],
            accept_multiple_files=True,
        )
        ws = col_b.text_input("워크스페이스 ID", value="upload-demo", key="ws_up")

        # 서버는 워크스페이스별 사본을 유지하고 '전체'를 다시 검사한다.
        # 이걸 안 보여주면 "파일 하나 올렸는데 왜 다른 파일 결과가 나오지?" 가 된다.
        try:
            kept = requests.get(f"{API}/api/workspace/{ws}/files", timeout=10).json()["files"]
        except Exception:
            kept = []
        if kept:
            with st.expander(f"⚠️ 이 워크스페이스에 이미 {len(kept)}개 파일이 있습니다 — 함께 검사됩니다"):
                st.code("\n".join(kept), language=None)
                if st.button("보관 파일 모두 지우고 새로 시작", key="reset"):
                    requests.delete(f"{API}/api/workspace/{ws}", timeout=15)
                    st.session_state.pop("result_up", None)
                    st.rerun()

        if st.button("검사 실행", type="primary", use_container_width=True,
                     disabled=not uploaded, key="run_up"):
            changes: list[dict] = []
            for up in uploaded:
                raw = up.getvalue()
                if up.name.lower().endswith(".zip"):
                    changes.extend(changes_from_zip(raw))
                else:
                    changes.append({
                        "path": up.name, "change_type": "created",
                        "content": raw.decode("utf-8", errors="ignore"),
                    })
            if not changes:
                st.warning("검사 대상 파일이 없습니다. 의존성 파일이나 소스 파일이 포함되어야 합니다.")
            else:
                st.caption(f"전송 {len(changes)}개: " + ", ".join(c["path"] for c in changes[:8]))
                result = run_scan("/api/scan", {
                    "workspace_id": ws, "changes": changes, "llm": use_llm, "doc_check": True,
                })
                if result:
                    st.session_state["result_up"] = result

        if st.session_state.get("result_up"):
            render_result(st.session_state["result_up"])

with tab_patent:
    st.markdown(
        "**기획서를 붙여넣으면** 유사한 선행 특허를 KIPRIS에서 찾아 대조합니다.  \n"
        "라이선스 검사와 파이프라인 구조는 같고, 대조 대상만 다릅니다."
    )
    st.warning(
        "이 결과는 **침해 판정이 아닙니다.** 초록만 보고 기술적 유사도를 본 것이며, "
        "청구범위는 확인하지 않았습니다. 변리사 조사가 필요한 지점을 표시하는 용도입니다.",
        icon="⚖️",
    )

    doc_file = st.file_uploader("기획서 파일 (.md / .txt)", type=["md", "txt"], key="pdoc")
    default_doc = doc_file.getvalue().decode("utf-8", errors="ignore") if doc_file else ""
    document = st.text_area(
        "기획서 본문", value=default_doc, height=220,
        placeholder="서비스가 무엇을 어떻게 하는지 기술 구성 중심으로 적을수록 검색이 정확해집니다.",
    )
    c1, c2 = st.columns(2)
    per_query = c1.slider("검색어당 수집 건수", 3, 15, 5)
    max_assess = c2.slider("유사도 판정 건수", 3, 20, 8,
                           help="많을수록 정확하지만 건당 6초 + KIPRIS 호출을 씁니다")

    if st.button("선행 특허 검토", type="primary", use_container_width=True,
                 disabled=len(document.strip()) < 50, key="run_pat"):
        with st.spinner("검색어 추출 → KIPRIS 검색 → 유사도 판정… 몇 분 걸립니다"):
            try:
                resp = requests.post(
                    f"{API}/api/screen-patent",
                    json={"document": document, "per_query": per_query,
                          "max_assess": max_assess},
                    timeout=1800,
                )
                if resp.status_code == 503:
                    st.error(f"KIPRIS 연동 불가\n\n{resp.json().get('detail', '')}")
                    st.stop()
                resp.raise_for_status()
                st.session_state["patent"] = resp.json()
            except Exception as exc:
                st.error(f"검토 실패: {exc}")

    pat = st.session_state.get("patent")
    if pat:
        st.divider()
        st.markdown(f"**추출된 핵심 아이디어**  \n{pat['core_idea_ko']}")

        m = st.columns(4)
        m[0].metric("검색 후보", f"{pat['candidates']}건")
        m[1].metric("판정 완료", f"{pat['assessed']}건")
        m[2].metric("미판정", f"{pat['not_assessed']}건")
        m[3].metric("유사 특허", f"{len(pat['findings'])}건")

        # 조용히 자르면 "전부 봤다"로 읽힌다. 반드시 표시한다.
        if pat["not_assessed"]:
            st.info(pat["coverage_note"], icon="ℹ️")

        with st.expander(f"검색어 {len(pat['queries'])}개와 결과 건수"):
            for q, s in zip(pat["queries"], pat["search_stats"]):
                total = s.get("total", "?")
                st.markdown(f"- `{s['query']}` — 전체 **{total}**건 · {q['aspect']}")
            st.caption("KIPRIS 검색은 단어를 AND로 묶습니다. 검색어가 길면 0건이 됩니다.")

        if not pat["findings"]:
            st.success("판정 대상 중 유의미한 유사 특허가 없습니다. (미판정 건은 별개입니다)")
        for f in pat["findings"]:
            mark = "🔴" if f["similarity"] == "HIGH" else "🟡"
            st.markdown(f"#### {mark} {f['similarity']} · `{f['application_number']}`")
            st.markdown(f"**{f['title_ko'] or f['title_en']}**")
            st.caption(
                f"출원인 {f['applicant'] or '-'} · 출원일 {f['application_date'] or '-'} · "
                f"IPC {f['ipc'][:60] or '-'}"
            )
            cols = st.columns(2)
            cols[0].markdown(f"**겹치는 부분**  \n{f['overlap_ko']}")
            cols[1].markdown(f"**다른 부분**  \n{f['difference_ko']}")
            flags = ["⚖️ 변리사 조사 필요"] if f["needs_expert_review"] else []
            flags.append("✅ 근거 기반" if f["grounded"] else "⚠️ 근거 부족")
            st.caption(" · ".join(flags) + f" · 매칭 검색어: {', '.join(f['matched_queries'])}")
            with st.expander("특허 초록 원문 (영문)"):
                st.text(f["abstract_en"] or "(초록 없음)")
                st.caption("출처: KIPRIS Plus 한국특허영문초록(KPA)")
            st.divider()

        st.download_button(
            "특허 검토 리포트 JSON",
            json.dumps(pat, ensure_ascii=False, indent=2),
            file_name="patent-screen.json", mime="application/json",
        )

with tab_timeline:
    ws_tl = st.text_input("워크스페이스 ID", value="local-folder", key="tl")
    if st.button("이력 조회"):
        try:
            data = requests.get(f"{API}/api/timeline/{ws_tl}", timeout=30).json()
        except Exception as exc:
            st.error(f"조회 실패: {exc}")
            st.stop()
        if "timeline" not in data:
            st.info("검사 이력이 없습니다.")
            st.stop()
        st.caption(f"검사 {data['scans']}회")
        for i, e in enumerate(data["timeline"], 1):
            tiers = " · ".join(f"{k} {v}" for k, v in sorted(e["by_tier"].items())) or "없음"
            st.markdown(f"**{i}차** — 총 {e['total']}건 ({tiers})")
            st.caption(e["scanned_at"])
            for loc in e.get("new_locators", []):
                st.markdown(f"&nbsp;&nbsp;🔺 **새 위험** `{loc}`", unsafe_allow_html=True)
            for loc in e.get("resolved_locators", []):
                st.markdown(f"&nbsp;&nbsp;✅ 해소 `{loc}`", unsafe_allow_html=True)
            st.divider()
