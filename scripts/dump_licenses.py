"""
한국저작권위원회 오픈소스SW 라이선스정보 서비스(_GW) 전량 덤프.

84종 라이선스의 요약/의무사항을 받아 RAG 색인용 JSON으로 저장한다.
  - getOpnSourSWLisncList     : 라이선스 목록 (licenseName, lid, summaryKo)
  - getOpnSourSWLisncImfeReq  : 의무사항 (keyFeature 산문 + opt*Yn 플래그)

출력: data/licenses.json
사용: python scripts/dump_licenses.py
"""

import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


import json
import os
import re
import sys
import time
import html
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

BASE = "https://apis.data.go.kr/B552546/OpnSourSWLisncInfoService"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "licenses.json"

# Decoding 키(원본)를 넣는다. requests가 params로 넘길 때 알아서 인코딩한다.
SERVICE_KEY = os.environ.get("COPYRIGHT_API_KEY", "")


def strip_html(raw: str) -> str:
    """summaryKo/keyFeature는 <P>, <BR>, &nbsp; 가 섞인 HTML 조각으로 온다."""
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<\s*/?\s*p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def call(operation: str, **params) -> ET.Element:
    params = {"serviceKey": SERVICE_KEY, **params}
    r = requests.get(f"{BASE}/{operation}", params=params, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    code = root.findtext(".//resultCode")
    if code != "00":
        raise RuntimeError(f"{operation} 실패: {code} / {root.findtext('.//resultMsg')}")
    return root


def fetch_list() -> list[dict]:
    root = call("getOpnSourSWLisncList", numOfRows=500, pageNo=1)
    total = int(root.findtext(".//totalCount") or 0)
    items = []
    for item in root.iter("item"):
        items.append(
            {
                "lid": item.findtext("lid"),
                "name": item.findtext("licenseName"),
                "related": item.findtext("relLicenseNm") or "",
                "summary_ko": strip_html(item.findtext("summaryKo") or ""),
            }
        )
    print(f"목록 {len(items)}건 수신 (totalCount={total})")
    return items


def fetch_obligations(lid: str) -> dict:
    """의무사항. opt*Yn 필드는 값이 'O'일 때만 응답에 나타난다 (부재 = 해당 없음)."""
    root = call("getOpnSourSWLisncImfeReq", lid=lid)
    item = next(root.iter("item"), None)
    if item is None:
        return {"key_feature": "", "flags": {}}
    flags = {}
    for child in item:
        if child.tag.startswith("opt") and child.tag.endswith("Yn"):
            if (child.text or "").strip() == "O":
                flags[child.tag] = True
    return {
        "key_feature": strip_html(item.findtext("keyFeature") or ""),
        "flags": flags,
    }


def main() -> int:
    if not SERVICE_KEY:
        print("COPYRIGHT_API_KEY 환경변수가 비어 있다. .env 를 확인할 것.", file=sys.stderr)
        return 1

    licenses = fetch_list()
    for i, lic in enumerate(licenses, 1):
        try:
            lic.update(fetch_obligations(lic["lid"]))
        except Exception as exc:  # 개별 실패가 전체를 막지 않게
            print(f"  ! lid={lic['lid']} {lic['name']}: {exc}", file=sys.stderr)
            lic.update({"key_feature": "", "flags": {}})
        if i % 10 == 0:
            print(f"  의무사항 {i}/{len(licenses)}")
        time.sleep(0.05)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(licenses, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장: {OUT} ({len(licenses)}종)")

    # 플래그 분포 — optFinishLicenseYn 이 copyleft 지표인지 확인하기 위한 근거
    dist: dict[str, int] = {}
    for lic in licenses:
        for flag in lic["flags"]:
            dist[flag] = dist.get(flag, 0) + 1
    print("\n[플래그 분포]")
    for flag, n in sorted(dist.items(), key=lambda kv: -kv[1]):
        print(f"  {flag:26s} {n:3d}종")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
