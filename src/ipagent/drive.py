"""Google Drive 작업공간 연동.

로컬 폴더 감시기가 못 하는 두 가지를 맡는다.
  1) Google 문서(Docs/Sheets)는 파일이 아니라 서비스 안의 객체다.
     Drive 데스크톱 앱으로 마운트해도 `.gdoc` 바로가기(링크만 든 JSON)로 보여
     내용을 읽을 수 없다. files.export 로 텍스트를 뽑아야 한다.
  2) 팀이 공유 드라이브에 올려둔 기획서를 각자 로컬에 내려받지 않아도 검사할 수 있다.

인증은 **서비스 계정**을 쓴다. OAuth 동의 화면·브라우저 로그인이 필요 없고,
검사할 폴더를 서비스 계정 이메일에 '뷰어'로 공유하기만 하면 된다.
읽기 전용 스코프만 요청한다.

준비:
  1. GCP 콘솔 > API 및 서비스 > 라이브러리 > Google Drive API 사용 설정
  2. IAM > 서비스 계정 > 만들기 > 키 추가(JSON) 내려받기
  3. 그 JSON 경로를 GOOGLE_DRIVE_CREDENTIALS 에 넣기
  4. 검사할 Drive 폴더를 서비스 계정 이메일에 공유(뷰어)
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass, field

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

FOLDER_MIME = "application/vnd.google-apps.folder"

# Google 문서는 파일이 아니라서 export 해야 한다.
# 문서는 마크다운으로 뽑으면 제목·목록 구조가 살아남는다. 안 되는 계정도 있어
# text/plain 으로 물러설 수 있게 후보를 순서대로 둔다.
EXPORT_AS = {
    "application/vnd.google-apps.document": (["text/markdown", "text/plain"], ".md"),
    "application/vnd.google-apps.spreadsheet": (["text/csv"], ".csv"),
    "application/vnd.google-apps.presentation": (["text/plain"], ".txt"),
}

# 그대로 내려받아도 되는 것들. 검사 대상이 아닌 바이너리는 건너뛴다.
DOWNLOAD_SUFFIXES = (
    ".md", ".txt", ".rst", ".json", ".toml", ".cfg", ".py", ".js", ".ts",
    ".java", ".go", ".c", ".h", ".cpp", ".rs",
)
DOWNLOAD_NAMES = {"requirements.txt", "pyproject.toml", "package.json", "LICENSE", "NOTICE"}

MAX_BYTES = 1_000_000


class DriveError(RuntimeError):
    pass


@dataclass
class DriveFile:
    id: str
    name: str
    path: str            # 폴더 구조를 반영한 상대 경로
    mime: str
    modified: str
    size: int = 0
    exported: bool = False
    detail: dict = field(default_factory=dict)


def _credentials():
    path = os.environ.get("GOOGLE_DRIVE_CREDENTIALS", "").strip()
    if not path:
        raise DriveError(
            "GOOGLE_DRIVE_CREDENTIALS 가 비어 있습니다. "
            "서비스 계정 JSON 키 경로를 .env 에 넣으세요."
        )
    if not os.path.exists(path):
        raise DriveError(f"서비스 계정 키 파일을 찾을 수 없습니다: {path}")
    try:
        from google.oauth2 import service_account
    except ImportError as exc:  # pragma: no cover
        raise DriveError("google-auth 가 설치되어 있지 않습니다") from exc
    return service_account.Credentials.from_service_account_file(path, scopes=SCOPES)


class DriveClient:
    def __init__(self) -> None:
        from googleapiclient.discovery import build

        self.creds = _credentials()
        # cache_discovery=False — 파일 캐시 경고를 피하고 기동을 단순하게 한다
        self.svc = build("drive", "v3", credentials=self.creds, cache_discovery=False)

    @property
    def account_email(self) -> str:
        return getattr(self.creds, "service_account_email", "")

    # ------------------------------------------------------------- 목록

    def walk(self, folder_id: str, prefix: str = "", depth: int = 0) -> list[DriveFile]:
        """폴더를 재귀적으로 훑는다. 공유 드라이브도 지원한다."""
        if depth > 6:
            return []
        out: list[DriveFile] = []
        token = None
        while True:
            try:
                resp = self.svc.files().list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id, name, mimeType, modifiedTime, size)",
                    pageSize=200, pageToken=token,
                    includeItemsFromAllDrives=True, supportsAllDrives=True,
                ).execute()
            except Exception as exc:
                raise DriveError(
                    f"폴더를 읽을 수 없습니다 ({folder_id}). "
                    f"서비스 계정({self.account_email})에 공유했는지 확인하세요. — {exc}"
                ) from exc

            for f in resp.get("files", []):
                name, mime = f["name"], f["mimeType"]
                rel = f"{prefix}{name}"
                if mime == FOLDER_MIME:
                    out.extend(self.walk(f["id"], prefix=f"{rel}/", depth=depth + 1))
                    continue
                out.append(DriveFile(
                    id=f["id"], name=name, path=rel, mime=mime,
                    modified=f.get("modifiedTime", ""), size=int(f.get("size") or 0),
                ))
            token = resp.get("nextPageToken")
            if not token:
                break
        return out

    # ------------------------------------------------------------- 내용

    def is_target(self, f: DriveFile) -> bool:
        if f.mime in EXPORT_AS:
            return True
        low = f.name.lower()
        return f.name in DOWNLOAD_NAMES or low.endswith(DOWNLOAD_SUFFIXES)

    def fetch_text(self, f: DriveFile) -> str | None:
        """Google 문서는 export, 일반 파일은 download."""
        from googleapiclient.http import MediaIoBaseDownload

        try:
            if f.mime in EXPORT_AS:
                candidates, _ = EXPORT_AS[f.mime]
                for mime_out in candidates:
                    try:
                        data = self.svc.files().export(fileId=f.id, mimeType=mime_out).execute()
                    except Exception:
                        continue  # 이 형식을 지원하지 않으면 다음 후보로
                    f.exported = True
                    f.detail["export_mime"] = mime_out
                    text = (data.decode("utf-8", errors="ignore")
                            if isinstance(data, bytes) else str(data))
                    # export 는 UTF-8 BOM 을 붙여 준다. 그대로 두면 첫 글자가 깨져 보인다.
                    return text.lstrip("﻿")
                return None

            if f.size and f.size > MAX_BYTES:
                return None
            buf = io.BytesIO()
            req = self.svc.files().get_media(fileId=f.id, supportsAllDrives=True)
            downloader = MediaIoBaseDownload(buf, req)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return buf.getvalue().decode("utf-8", errors="ignore").lstrip("﻿")
        except Exception:
            return None

    # ------------------------------------------------------------- 변환

    def folder_info(self, folder_id: str) -> dict:
        """폴더 이름과 검사 대상 개수. 붙여넣은 링크가 맞는지 확인하는 용도다.

        서비스 계정은 '공유 문서함' 이 없어 공유받은 폴더를 목록으로 열거할 수 없다.
        (files.list 는 0건을 돌려준다) ID 로 직접 접근만 가능하므로,
        사용자가 링크를 붙여넣으면 이름을 보여줘 맞는 폴더인지 확인시켜 준다.
        """
        try:
            meta = self.svc.files().get(
                fileId=folder_id, fields="id, name, mimeType, modifiedTime",
                supportsAllDrives=True,
            ).execute()
        except Exception as exc:
            raise DriveError(
                f"폴더에 접근할 수 없습니다. 서비스 계정({self.account_email})에 "
                f"'뷰어' 로 공유했는지 확인하세요. — {exc}"
            ) from exc

        if meta.get("mimeType") != FOLDER_MIME:
            raise DriveError(f"폴더가 아닙니다: {meta.get('name')}")

        files = self.walk(folder_id)
        targets = [f for f in files if self.is_target(f)]
        docs = [f for f in targets if f.mime in EXPORT_AS]
        return {
            "id": folder_id,
            "name": meta.get("name", ""),
            "modified": meta.get("modifiedTime", "")[:10],
            "total_files": len(files),
            "targets": len(targets),
            "google_docs": len(docs),
            "sample": [f.path for f in targets[:8]],
        }

    def snapshot(self, folder_id: str) -> dict[str, str]:
        """{경로: 수정시각}. 변경 여부만 싸게 확인하려고 쓴다.

        내용을 받지 않으므로 폴더 수만큼의 목록 조회로 끝난다.
        폴링 주기마다 이것만 돌리고, 달라졌을 때만 본문을 내려받는다.
        """
        return {f.path: f.modified for f in self.walk(folder_id) if self.is_target(f)}

    def collect_changes(self, folder_id: str) -> tuple[list[dict], dict]:
        """검사 서버가 받는 changes 형식으로 바꾼다.

        로컬 감시기·업로드와 같은 형식이라 백엔드는 출처를 구분할 필요가 없다.
        """
        files = self.walk(folder_id)
        changes, skipped = [], []
        exported = 0
        for f in files:
            if not self.is_target(f):
                skipped.append(f.name)
                continue
            text = self.fetch_text(f)
            if text is None:
                skipped.append(f.name)
                continue
            path = f.path
            if f.mime in EXPORT_AS:
                # Google 문서는 확장자가 없다. 검사기가 문서로 인식하도록 붙여준다.
                path = path + EXPORT_AS[f.mime][1]
                exported += 1
            changes.append({"path": path, "change_type": "modified", "content": text})

        return changes, {
            "total_files": len(files),
            "collected": len(changes),
            "google_docs_exported": exported,
            "skipped": skipped[:20],
            "service_account": self.account_email,
        }


def parse_folder_id(raw: str) -> str:
    """폴더 ID 또는 공유 URL 을 받아 ID 만 돌려준다."""
    raw = raw.strip()
    if "/folders/" in raw:
        raw = raw.split("/folders/", 1)[1]
    if "id=" in raw:
        raw = raw.split("id=", 1)[1]
    return raw.split("?")[0].split("/")[0].strip()
