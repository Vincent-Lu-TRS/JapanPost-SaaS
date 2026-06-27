"""
Google Drive PDF 上傳模組
支援共用雲端硬碟（supportsAllDrives=True）
"""
import os
import io
import logging
import re
import streamlit as st
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

DRIVE_FOLDER_ID = "1_JYIwmtpKQ7FjWY2zplofLGe0GHaEMvw"
SCOPES = ["https://www.googleapis.com/auth/drive"]


def _get_drive_service():
    try:
        creds_info = dict(st.secrets["gcp_service_account"])
    except Exception:
        import json
        creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")
        with open(creds_path, "r", encoding="utf-8") as f:
            creds_info = json.load(f)

    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def upload_pdf(pdf_bytes: bytes, filename: str, folder_id: str = DRIVE_FOLDER_ID, log_cb=None) -> str | None:
    """
    將 PDF 二進位資料上傳至指定 Google Drive 資料夾。
    回傳上傳後的 file ID，失敗則回傳 None。
    """
    def _log(msg):
        if log_cb:
            log_cb(msg)
        else:
            logging.info(msg)

    try:
        service = _get_drive_service()
        file_metadata = {
            "name": filename,
            "parents": [folder_id],
        }
        media = MediaIoBaseUpload(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            resumable=False,
        )
        file = (
            service.files()
            .create(
                body=file_metadata,
                media_body=media,
                fields="id, name",
                supportsAllDrives=True,  # 關鍵：支援共用雲端硬碟
            )
            .execute()
        )
        file_id = file.get("id")
        _log(f"☁️ PDF 上傳成功：{filename}（ID: {file_id}）")
        return file_id
    except Exception as e:
        _log(f"❌ Google Drive 上傳失敗: {e}")
        return None


def list_drive_files(folder_id: str, name_prefix: str = "") -> list[dict]:
    """List files in a Drive folder, optionally filtering by name prefix."""
    service = _get_drive_service()
    escaped_prefix = name_prefix.replace("'", "\\'")
    query_parts = [
        f"'{folder_id}' in parents",
        "trashed = false",
    ]
    if escaped_prefix:
        query_parts.append(f"name contains '{escaped_prefix}'")
    query = " and ".join(query_parts)
    files: list[dict] = []
    page_token = None
    while True:
        response = (
            service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name, webViewLink)",
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return files


def next_sequence_filename(existing_files: list[dict], today: str | None = None) -> str:
    """Return YYMMDD-{n}揀貨標籤.pdf using max existing sequence + 1."""
    from datetime import date

    if today is None:
        yymmdd = date.today().strftime("%y%m%d")
    else:
        yymmdd = str(today).replace("-", "")[2:8]

    pattern = re.compile(rf"^{re.escape(yymmdd)}-(\d+)揀貨標籤\.pdf$")
    max_sequence = 0
    for file in existing_files:
        match = pattern.match(str(file.get("name", "")))
        if match:
            max_sequence = max(max_sequence, int(match.group(1)))
    return f"{yymmdd}-{max_sequence + 1}揀貨標籤.pdf"


def choose_safe_picking_filename(
    initial_files: list[dict],
    rechecked_files: list[dict],
    today: str | None = None,
    timestamp: str | None = None,
) -> str:
    """Choose a daily sequence filename, falling back to timestamp on race collision."""
    candidate = next_sequence_filename(initial_files, today=today)
    existing_names = {str(file.get("name", "")) for file in rechecked_files}
    if candidate not in existing_names:
        return candidate

    from datetime import datetime

    if today is None:
        yymmdd = datetime.now().strftime("%y%m%d")
    else:
        yymmdd = str(today).replace("-", "")[2:8]
    timestamp = timestamp or datetime.now().strftime("%H%M%S")
    fallback = f"{yymmdd}-{timestamp}揀貨標籤.pdf"
    suffix = 1
    while fallback in existing_names:
        fallback = f"{yymmdd}-{timestamp}-{suffix}揀貨標籤.pdf"
        suffix += 1
    return fallback


def upload_file_to_drive(local_path: str, folder_id: str, mime_type: str = "application/pdf") -> dict:
    """Upload a local file to Drive and return id/name/webViewLink metadata."""
    service = _get_drive_service()
    filename = os.path.basename(local_path)
    with open(local_path, "rb") as f:
        media = MediaIoBaseUpload(io.BytesIO(f.read()), mimetype=mime_type, resumable=False)
    return (
        service.files()
        .create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media,
            fields="id, name, webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )
