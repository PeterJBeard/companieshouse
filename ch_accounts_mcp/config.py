"""Runtime settings, read from environment variables once."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    companies_house_api_key: str | None = field(repr=False, default=None)
    anthropic_api_key: str | None = field(repr=False, default=None)
    mcp_bearer_token: str | None = field(repr=False, default=None)
    cache_dir: Path = Path.home() / ".cache" / "ch_accounts_mcp"
    ocr_device: str = "cpu"
    ocr_engine: str = "rapidocr"
    ocr_workers: int = 1
    public_base_url: str = "http://127.0.0.1:8765"
    viewer_port: int = 8765
    start_viewer: bool = True

    def redacted(self) -> dict:
        return {
            "companies_house_api_key": "set" if self.companies_house_api_key else "unset",
            "anthropic_api_key": "set" if self.anthropic_api_key else "unset",
            "mcp_bearer_token": "set" if self.mcp_bearer_token else "unset",
            "cache_dir": str(self.cache_dir),
            "ocr_device": self.ocr_device,
            "ocr_engine": self.ocr_engine,
            "ocr_workers": self.ocr_workers,
            "public_base_url": self.public_base_url,
        }


def load_settings() -> Settings:
    device = os.environ.get("OCR_DEVICE", "cpu").lower()
    default_engine = "paddleocr" if device == "cuda" else "rapidocr"
    port = _int("VIEWER_PORT", 8765)
    return Settings(
        companies_house_api_key=os.environ.get("COMPANIES_HOUSE_API_KEY") or None,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        mcp_bearer_token=os.environ.get("MCP_BEARER_TOKEN") or None,
        cache_dir=Path(os.environ.get("CACHE_DIR") or Path.home() / ".cache" / "ch_accounts_mcp"),
        ocr_device=device,
        ocr_engine=os.environ.get("OCR_ENGINE", default_engine).lower(),
        ocr_workers=max(1, _int("OCR_WORKERS", os.cpu_count() or 1)),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", f"http://127.0.0.1:{port}").rstrip("/"),
        viewer_port=port,
        start_viewer=os.environ.get("START_VIEWER", "1") not in ("0", "false", "no"),
    )


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def override_settings(s: Settings) -> None:
    global _settings
    _settings = s
