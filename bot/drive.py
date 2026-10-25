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
HISTORICAL_LABEL_FOLDER_ID = "1RZ0hxcRxwjNomTTfsznAsBmfumGOzdhe"
ORIGINAL_LABEL_FOLDER_IDS = frozenset((HISTORICAL_LABEL_FOLDER_ID, DRIVE_FOLDER_ID))
_ORIGINAL_LABEL_METADATA = "id,name,mimeType,parents,trashed,shortcutDetails"
_ORIGINAL_LABEL_MAX_PAGES = 5
_ORIGINAL_LABEL_MAX_GETS = 20


def _valid_drive_file_id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value) is not None


def _validated_original_pdf_url(metadata, expected_id=None):
    """Validate direct PDF metadata; never follow a shortcut or trust a supplied URL."""
    if not isinstance(metadata, dict):
        return None
    file_id = metadata.get("id")
    if not _valid_drive_file_id(file_id) or (expected_id is not None and file_id != expected_id):
        return None
    parents = metadata.get("parents")
    if (metadata.get("mimeType") != "application/pdf"
            or metadata.get("trashed") is not False
            or "shortcutDetails" in metadata
            or not isinstance(parents, list)
            or not any(parent in ORIGINAL_LABEL_FOLDER_IDS for parent in parents if isinstance(parent, str))):
        return None
    return f"https://drive.google.com/file/d/{file_id}/view"


def _find_original_pdf_candidates(service, order_id):
    """Return complete, bounded metadata candidates, or None when completeness is unknown."""
    # Inputs are validated too; escape Drive query literals independently of that policy.
    def literal(value):
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"

    folders = " or ".join(f"{literal(folder)} in parents" for folder in sorted(ORIGINAL_LABEL_FOLDER_IDS))
    # `name contains` is prefix-only in Drive. A fullText phrase also matches name
    # tokens; Drive explicitly treats underscores as spaces in quoted phrases.
    # Search may include content-index matches, so local filename validation remains
    # authoritative. No content or description is requested/downloaded.
    order_phrase = '"' + order_id.replace("_", " ") + '"'
    query = (f"({folders}) and trashed = false and mimeType = 'application/pdf'"
             f" and fullText contains {literal(order_phrase)}")
    candidates = []
    page_token = None
    seen_tokens = set()
    try:
        for _page in range(_ORIGINAL_LABEL_MAX_PAGES):
            response = service.files().list(
                q=query, fields=f"nextPageToken,incompleteSearch,files({_ORIGINAL_LABEL_METADATA})",
                pageToken=page_token, pageSize=100, supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            if not isinstance(response, dict) or response.get("incompleteSearch"):
                return None
            files = response.get("files", [])
            if not isinstance(files, list):
                return None
            candidates.extend(files)
            page_token = response.get("nextPageToken")
            if not page_token:
                return candidates
            if not isinstance(page_token, str) or page_token in seen_tokens:
                return None
            seen_tokens.add(page_token)
    except Exception:
        return None
    return None


def _original_filename_matches(name, order_id, tracking):
    """Accept only a unique order/tracking interpretation across both producer grammars."""
    if not isinstance(name, str) or not name.lower().endswith(".pdf"):
        return False
    stem = name[:-4]
    candidates = set()
    # Legacy producer uses an actual space between content and the complete
    # order_tracking suffix. Punctuation/underscores inside that order are retained.
    if " " in stem:
        content, suffix = stem.rsplit(" ", 1)
        if content and "_" in suffix:
            legacy_order, legacy_tracking = suffix.rsplit("_", 1)
            if (re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", legacy_order)
                    and re.fullmatch(r"[A-Za-z0-9-]{1,80}", legacy_tracking)):
                candidates.add((legacy_order, legacy_tracking))
    # Current producer uses content_order_tracking_recipient. Enumerate every
    # legal order boundary instead of assuming the last underscore belongs to item.
    # Do not begin from the requested tracking: a recipient can itself contain
    # " order_tracking", while the actual current label belongs to another order.
    # Generic legal tracking tokens are considered too, rather than guessing that
    # only a postal-looking token is authoritative. Multiple interpretations hide.
    parts = stem.split("_")
    for tracking_index in range(2, len(parts) - 1):
        candidate_tracking = parts[tracking_index]
        if (not re.fullmatch(r"[A-Za-z0-9-]{1,80}", candidate_tracking)
                or not "_".join(parts[tracking_index + 1:])):
            continue
        for order_index in range(1, tracking_index):
            if not "_".join(parts[:order_index]):
                continue
            candidate_order = "_".join(parts[order_index:tracking_index])
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", candidate_order):
                candidates.add((candidate_order, candidate_tracking))
    return candidates == {(order_id, tracking)}


def resolve_original_label_urls(order_id, tracking_numbers, *, shipments=(), service=None) -> dict[str, str]:
    """Resolve only requested original labels using trusted ledger links, then exact filenames.

    `shipments` must come from server-filtered history for this order. This reads metadata
    only. Missing access, ambiguous matches and incomplete searches omit that tracking;
    callers can hide the link without exposing Drive metadata or service exceptions.
    """
    if not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", order_id):
        return {}
    if isinstance(tracking_numbers, (str, bytes)):
        return {}
    try:
        tracking_values = list(dict.fromkeys(tracking_numbers))
        ledger_shipments = list(shipments)
    except (TypeError, ValueError):
        return {}
    if (not tracking_values or len(tracking_values) > 50 or any(
            not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", value)
            for value in tracking_values)):
        return {}
    try:
        if service is None:
            service = _get_drive_service()
    except Exception:
        return {}

    resolved = {}
    unresolved = []
    metadata_cache = {}
    for tracking in tracking_values:
        ledger_ids = {shipment.get("drive_file_id") for shipment in ledger_shipments
                      if isinstance(shipment, dict) and shipment.get("tracking") == tracking
                      and _valid_drive_file_id(shipment.get("drive_file_id"))}
        ledger_urls = set()
        unavailable = False
        for file_id in sorted(ledger_ids):
            if file_id not in metadata_cache:
                if len(metadata_cache) >= _ORIGINAL_LABEL_MAX_GETS:
                    unavailable = True
                    break
                try:
                    metadata_cache[file_id] = service.files().get(
                        fileId=file_id, fields=_ORIGINAL_LABEL_METADATA,
                        supportsAllDrives=True).execute()
                except Exception:
                    # 404 can conceal missing access too; never infer absence from
                    # an exception, or choose a visible subset of ledger links.
                    metadata_cache[file_id] = None
            metadata = metadata_cache[file_id]
            if not isinstance(metadata, dict):
                unavailable = True
                break
            url = _validated_original_pdf_url(metadata, expected_id=file_id)
            if url:
                ledger_urls.add(url)
        if unavailable:
            continue
        if ledger_urls:
            if len(ledger_urls) == 1:
                resolved[tracking] = ledger_urls.pop()
            continue
        unresolved.append(tracking)

    # One complete order query serves all unresolved tracking numbers. Together
    # with the shared get cache this caps the entire call at 20 gets + 5 lists.
    candidates = _find_original_pdf_candidates(service, order_id) if unresolved else None
    if candidates is None:
        return resolved
    for tracking in unresolved:
        urls = set()
        for metadata in candidates:
            url = _validated_original_pdf_url(metadata)
            if url and _original_filename_matches(metadata.get("name"), order_id, tracking):
                urls.add(url)
        if len(urls) == 1:
            resolved[tracking] = urls.pop()
    return resolved


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
        _log(f"❌ Google Drive 上傳失敗: {type(e).__name__}")
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

    pattern = re.compile(rf"^{re.escape(yymmdd)}-(\d+)揀貨標籤(?:\.pdf)?$")
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
    """Choose a daily sequence filename, retrying sequence numbers before timestamp fallback."""
    combined_files = list(initial_files) + list(rechecked_files)
    existing_names = {str(file.get("name", "")) for file in combined_files}
    candidate = next_sequence_filename(combined_files, today=today)
    if candidate not in existing_names:
        return candidate

    for _attempt in range(1000):
        combined_files.append({"name": candidate})
        candidate = next_sequence_filename(combined_files, today=today)
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
