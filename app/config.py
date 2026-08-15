"""
Single place where environment configuration is read.

Everything is resolved through properties rather than captured at import time, so
config stays correct no matter when load_dotenv() runs relative to imports, and
tests can monkeypatch os.environ without reimporting modules.
"""

import os
import secrets
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Generated once per process and used only when APP_SESSION_SECRET is unset.
_EPHEMERAL_SECRET = secrets.token_urlsafe(32)

# Repo root (one level above this package)
BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

# Real environment variables win over .env, which is what production needs:
# Vercel/Azure inject config as real env vars.
load_dotenv(BASE_DIR / ".env")


class Settings:
    # --- Azure OpenAI ----------------------------------------------------
    @property
    def openai_base_url(self) -> Optional[str]:
        return os.getenv("AZURE_OPENAI_BASE_URL")

    @property
    def openai_api_key(self) -> Optional[str]:
        return os.getenv("AZURE_OPENAI_API_KEY")

    @property
    def openai_deployment(self) -> str:
        """Azure *deployment* name, which is not necessarily the model name."""
        return os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o_latest")

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key and self.openai_base_url)

    # --- Blob storage ----------------------------------------------------
    @property
    def blob_connection_string(self) -> Optional[str]:
        return os.getenv("DOC_PARSER_BLOB_CONNECTION_STRING")

    @property
    def blob_container(self) -> Optional[str]:
        return os.getenv("DOC_PARSER_BLOB_CONTAINER_NAME")

    @property
    def blob_upload_prefix(self) -> str:
        return os.getenv("BLOB_UPLOAD_PREFIX", "financial-agent/uploads").strip("/")

    @property
    def blob_output_prefix(self) -> str:
        return os.getenv("BLOB_OUTPUT_PREFIX", "financial-agent/outputs").strip("/")

    @property
    def blob_configured(self) -> bool:
        return bool(self.blob_connection_string and self.blob_container)

    # --- Evalueserve IDP document parser ---------------------------------
    @property
    def idp_base_url(self) -> str:
        return os.getenv(
            "IDP_BASE_URL",
            "https://gaidocparserdev.evalueserve.com/api/DocumentParser",
        ).rstrip("/")

    @property
    def idp_parse_path(self) -> str:
        return os.getenv("IDP_PARSE_PATH", "parseDocument").lstrip("/")

    @property
    def idp_status_path(self) -> str:
        return os.getenv("IDP_STATUS_PATH", "getStatusCountByRequestId").lstrip("/")

    @property
    def idp_product_client_id(self) -> str:
        return os.getenv("IDP_PRODUCT_CLIENT_ID", "9")

    @property
    def idp_product_project_id(self) -> str:
        return os.getenv("IDP_PRODUCT_PROJECT_ID", "9999")

    @property
    def idp_key(self) -> str:
        """The parser's "Key" payload field."""
        return os.getenv("IDP_KEY", "A")

    @property
    def idp_aira_request_id(self) -> str:
        return os.getenv("IDP_AIRA_REQUEST_ID", "")

    @property
    def idp_advance_parsing(self) -> Optional[bool]:
        """
        Send IsAdvanceParsing in the payload. Unset omits the field entirely.

        Testing showed the service reported IsAdvanceParsing=true either way and
        produced byte-identical output, so this is exposed for future use rather than
        because it currently changes anything.
        """
        raw = os.getenv("IDP_ADVANCE_PARSING")
        if raw is None or raw.strip() == "":
            return None
        return raw.strip().lower() in ("1", "true", "yes", "on")

    @property
    def idp_request_timeout(self) -> int:
        return int(os.getenv("IDP_REQUEST_TIMEOUT_SECONDS", "120"))

    @property
    def idp_locator(self) -> tuple:
        """(LocatorType, LocatorSubType, LocatorId) - these prefix the output filename."""
        return (
            os.getenv("IDP_LOCATOR_TYPE", "00"),
            os.getenv("IDP_LOCATOR_SUBTYPE", "0"),
            os.getenv("IDP_LOCATOR_ID", "00"),
        )

    @property
    def idp_poll_timeout(self) -> int:
        return int(os.getenv("IDP_POLL_TIMEOUT_SECONDS", "300"))

    @property
    def idp_poll_interval(self) -> float:
        return float(os.getenv("IDP_POLL_INTERVAL_SECONDS", "3"))

    @property
    def idp_work_prefix(self) -> str:
        """Where submitted PDFs and parser output live inside the container."""
        return os.getenv("IDP_WORK_PREFIX", "financial-agent/idp").strip("/")

    @property
    def document_locale(self) -> str:
        """
        How figures and dates are written in the documents this team receives.

        The analyst knows the source, so this is the source of truth. Detection runs
        alongside it only to flag a document that disagrees. US | UK | IN | EU | CH |
        ISO | AUTO.
        """
        return os.getenv("DOCUMENT_LOCALE", "US").upper()

    @property
    def batch_workers(self) -> int:
        """
        How many parser responses are processed concurrently in a batch.

        Every document is submitted to the parser first regardless, so this only bounds
        the local work of reconciling and building each result.
        """
        return int(os.getenv("BATCH_WORKERS", "6"))

    @property
    def batch_max_files(self) -> int:
        return int(os.getenv("BATCH_MAX_FILES", "50"))

    # --- Agent behaviour --------------------------------------------------
    @property
    def max_repair_rounds(self) -> int:
        """
        How many times the agent may repair-and-reverify.

        Each round applies one repair strategy and re-runs reconciliation, so this
        bounds the loop rather than the number of fixes.
        """
        return int(os.getenv("AGENT_MAX_REPAIR_ROUNDS", "6"))

    @property
    def reconcile_tolerance(self) -> float:
        """Absolute currency tolerance when comparing a computed total to a printed one."""
        return float(os.getenv("RECONCILE_TOLERANCE", "0.02"))

    @property
    def max_file_size_bytes(self) -> int:
        return int(os.getenv("MAX_FILE_SIZE_MB", "50")) * 1024 * 1024

    # --- Access ----------------------------------------------------------
    # A typed name: enough to attribute an approval to a person, not enough to prove
    # one. See app/auth.py for what this is and is not.
    @property
    def session_secret(self) -> str:
        """
        Key the session cookie is signed with.

        Unset generates one per process, so sessions do not survive a restart. That is
        the right default: a fixed fallback baked into the source would sign cookies
        anyone with the repo could forge.
        """
        return os.getenv("APP_SESSION_SECRET") or _EPHEMERAL_SECRET


settings = Settings()
