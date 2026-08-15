"""
Azure Blob Storage layer.

Source PDFs go to the upload prefix, generated JSON/CSV/Excel to the output prefix.
The container is shared with other projects, so everything written here stays
namespaced under those prefixes.
"""

import json
import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from azure.storage.blob import BlobServiceClient, ContentSettings

from .config import settings

logger = logging.getLogger(__name__)


def _encode(value: Any) -> Any:
    """
    Dates reach here as real date objects, which json.dumps refuses.

    Without this the extraction record silently failed to save: the failure was caught
    and logged as a warning, extraction still returned 200, and the problem only showed
    up later as "no stored extraction for job_id" when an analyst approved a fix - and
    because the approval never completed, the same fix was proposed on every upload.
    """
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

CONTENT_TYPES = {
    "json": "application/json",
    "csv": "text/csv",
    "excel": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

EXTENSIONS = {"json": "json", "csv": "csv", "excel": "xlsx"}


class BlobStore:
    """Thin wrapper over the two prefixes this app owns."""

    def __init__(self):
        self._service: Optional[BlobServiceClient] = None

    @property
    def configured(self) -> bool:
        return settings.blob_configured

    @property
    def container_name(self) -> Optional[str]:
        return settings.blob_container

    @property
    def upload_prefix(self) -> str:
        return settings.blob_upload_prefix

    @property
    def output_prefix(self) -> str:
        return settings.blob_output_prefix

    @property
    def service(self) -> BlobServiceClient:
        if not self.configured:
            raise RuntimeError(
                "Blob storage is not configured. Set DOC_PARSER_BLOB_CONNECTION_STRING "
                "and DOC_PARSER_BLOB_CONTAINER_NAME."
            )
        if self._service is None:
            self._service = BlobServiceClient.from_connection_string(
                settings.blob_connection_string
            )
        return self._service

    def _container(self):
        return self.service.get_container_client(self.container_name)

    def _put(self, blob_name: str, data: bytes, content_type: str) -> str:
        self._container().upload_blob(
            name=blob_name,
            data=data,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )
        logger.info(f"Uploaded {blob_name} ({len(data)} bytes)")
        return blob_name

    def _get(self, blob_name: str) -> bytes:
        return self._container().download_blob(blob_name).readall()

    @staticmethod
    def safe_name(filename: str) -> str:
        """Strip any path components a client may have sent."""
        return filename.replace("\\", "/").rsplit("/", 1)[-1] or "document.pdf"

    # Backwards-compatible alias
    _safe_name = safe_name

    def put_bytes(self, blob_name: str, data: bytes, content_type: str) -> str:
        """Write to an explicit blob path. Used for the parser's working files."""
        return self._put(blob_name, data, content_type)

    # --- source PDFs ------------------------------------------------------

    def put_source_pdf(self, job_id: str, filename: str, data: bytes) -> str:
        blob_name = f"{self.upload_prefix}/{job_id}__{self.safe_name(filename)}"
        return self._put(blob_name, data, "application/pdf")

    # --- extraction records ----------------------------------------------

    def put_record(self, job_id: str, record: Dict[str, Any]) -> str:
        """Persist the flat record so exports survive a serverless cold start."""
        blob_name = f"{self.output_prefix}/records/{job_id}.json"
        payload = json.dumps(record, indent=2, default=_encode).encode("utf-8")
        return self._put(blob_name, payload, CONTENT_TYPES["json"])

    def get_record(self, job_id: str) -> Dict[str, Any]:
        blob_name = f"{self.output_prefix}/records/{job_id}.json"
        return json.loads(self._get(blob_name).decode("utf-8"))

    # --- generated outputs ------------------------------------------------

    def put_output(self, fmt: str, basename: str, data: bytes) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        blob_name = f"{self.output_prefix}/{fmt}/{stamp}__{basename}.{EXTENSIONS[fmt]}"
        return self._put(blob_name, data, CONTENT_TYPES[fmt])

    def get_output(self, blob_name: str) -> bytes:
        if not blob_name.startswith(self.output_prefix + "/") or ".." in blob_name:
            raise ValueError("Refusing to read a blob outside the output prefix")
        return self._get(blob_name)

    # --- exact paths this app already recorded -----------------------------

    def get_by_blob_name(self, blob_name: str) -> bytes:
        """
        Read a blob at a path this app wrote earlier and stored in a record - for
        example Document.parser_output_blob, re-read by /api/rerun to reprocess a
        document from its original parse.

        No prefix guard, unlike get_output(): the caller here is always a path this
        app generated and persisted, never one a client supplied directly. A client-
        supplied path (get_output's blob= query param) is the case that needs guarding.
        """
        return self._get(blob_name)

    def list_outputs(self, fmt: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        prefix = f"{self.output_prefix}/{fmt}/" if fmt else f"{self.output_prefix}/"
        items = []
        for blob in self._container().list_blobs(name_starts_with=prefix):
            if "/records/" in blob.name:
                continue
            items.append({
                "name": blob.name,
                "size": blob.size,
                "last_modified": blob.last_modified.isoformat() if blob.last_modified else None,
            })
            if len(items) >= limit:
                break
        items.sort(key=lambda x: x["last_modified"] or "", reverse=True)
        return items


store = BlobStore()
