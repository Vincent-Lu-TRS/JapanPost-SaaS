"""
Google Drive PDF 上傳模組
支援共用雲端硬碟（supportsAllDrives=True）
"""
import os
import io
import logging
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
                supportsAllDrives=True,
            )
            .execute()
        )
        file_id = file.get("id")
        _log(f"☁️ PDF 上傳成功：{len(pdf_bytes)} Bytes}（ID: {file_id}）")
        return file_id
    except Exception as e:
        _log(f"❌ Google Drive 上傳失敗: {e}")
        return None
