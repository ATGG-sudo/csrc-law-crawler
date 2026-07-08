"""Runtime settings loaded from environment variables."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys


DEFAULT_OUTPUT_ROOT = Path("csrc-output")
DEFAULT_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
DEFAULT_CONFIG_FILE = Path("csrc_crawler_config.json")
DEFAULT_DELAY_MIN = 1.8
DEFAULT_DELAY_MAX = 3.6
DEFAULT_BATCH_SIZE = 40
DEFAULT_BATCH_PAUSE_MIN = 8.0
DEFAULT_BATCH_PAUSE_MAX = 15.0
DEFAULT_MAX_RETRIES = 5
DEFAULT_RETRY_BACKOFF_BASE = 5.0
DEFAULT_WORKERS = 1


def _bool_from_env(value: str | None, *, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean environment value: {value!r}")


def _int_from_value(value: object | None, *, default: int) -> int:
    if value is None:
        return default
    text = str(value).strip()
    if text == "":
        return default
    result = int(text)
    if result <= 0:
        raise ValueError("integer environment values must be positive")
    return result


def _float_from_value(value: object | None, *, default: float) -> float:
    if value is None:
        return default
    text = str(value).strip()
    if text == "":
        return default
    result = float(text)
    if result < 0:
        raise ValueError("float environment values must be non-negative")
    return result


def _cli_value(name: str) -> str | None:
    prefix = f"{name}="
    args = sys.argv[1:]
    for index, arg in enumerate(args):
        if arg == name and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith(prefix):
            return arg[len(prefix) :]
    return None


def _config_path() -> Path | None:
    raw = _cli_value("--config") or os.environ.get("CSRC_CONFIG_FILE")
    if raw:
        return Path(raw).expanduser()
    if DEFAULT_CONFIG_FILE.exists():
        return DEFAULT_CONFIG_FILE
    return None


def _load_config() -> dict[str, object]:
    path = _config_path()
    if path is None or not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"config file must contain a JSON object: {path}")
    return data


@dataclass(frozen=True)
class Settings:
    output_root: Path = DEFAULT_OUTPUT_ROOT
    max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES
    amac_verify_tls: bool = True
    delay_min: float = DEFAULT_DELAY_MIN
    delay_max: float = DEFAULT_DELAY_MAX
    batch_size: int = DEFAULT_BATCH_SIZE
    batch_pause_min: float = DEFAULT_BATCH_PAUSE_MIN
    batch_pause_max: float = DEFAULT_BATCH_PAUSE_MAX
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_backoff_base: float = DEFAULT_RETRY_BACKOFF_BASE
    workers: int = DEFAULT_WORKERS

    @classmethod
    def from_env(cls) -> "Settings":
        config = _load_config()
        output_root = (
            _cli_value("--output-root")
            or os.environ.get("CSRC_OUTPUT_ROOT")
            or os.environ.get("OUTPUT_ROOT")
            or config.get("output_root")
        )
        max_download_bytes = (
            _cli_value("--max-download-bytes")
            or os.environ.get("CSRC_MAX_DOWNLOAD_BYTES")
            or config.get("max_download_bytes")
        )
        amac_verify_tls = (
            os.environ.get("CSRC_AMAC_VERIFY_TLS")
            if os.environ.get("CSRC_AMAC_VERIFY_TLS") is not None
            else config.get("amac_verify_tls")
        )
        delay_min = (
            _cli_value("--delay-min")
            or os.environ.get("CSRC_DELAY_MIN")
            or config.get("delay_min")
        )
        delay_max = (
            _cli_value("--delay-max")
            or os.environ.get("CSRC_DELAY_MAX")
            or config.get("delay_max")
        )
        batch_size = (
            _cli_value("--batch-size")
            or os.environ.get("CSRC_BATCH_SIZE")
            or config.get("batch_size")
        )
        batch_pause_min = (
            _cli_value("--batch-pause-min")
            or os.environ.get("CSRC_BATCH_PAUSE_MIN")
            or config.get("batch_pause_min")
        )
        batch_pause_max = (
            _cli_value("--batch-pause-max")
            or os.environ.get("CSRC_BATCH_PAUSE_MAX")
            or config.get("batch_pause_max")
        )
        max_retries = (
            _cli_value("--max-retries")
            or os.environ.get("CSRC_MAX_RETRIES")
            or config.get("max_retries")
        )
        retry_backoff_base = (
            _cli_value("--retry-backoff-base")
            or os.environ.get("CSRC_RETRY_BACKOFF_BASE")
            or config.get("retry_backoff_base")
        )
        workers = (
            _cli_value("--workers")
            or os.environ.get("CSRC_WORKERS")
            or config.get("workers")
        )
        return cls(
            output_root=Path(str(output_root)).expanduser()
            if output_root
            else DEFAULT_OUTPUT_ROOT,
            max_download_bytes=_int_from_value(
                max_download_bytes,
                default=DEFAULT_MAX_DOWNLOAD_BYTES,
            ),
            amac_verify_tls=_bool_from_env(
                str(amac_verify_tls) if amac_verify_tls is not None else None,
                default=True,
            ),
            delay_min=_float_from_value(delay_min, default=DEFAULT_DELAY_MIN),
            delay_max=_float_from_value(delay_max, default=DEFAULT_DELAY_MAX),
            batch_size=_int_from_value(batch_size, default=DEFAULT_BATCH_SIZE),
            batch_pause_min=_float_from_value(
                batch_pause_min,
                default=DEFAULT_BATCH_PAUSE_MIN,
            ),
            batch_pause_max=_float_from_value(
                batch_pause_max,
                default=DEFAULT_BATCH_PAUSE_MAX,
            ),
            max_retries=_int_from_value(max_retries, default=DEFAULT_MAX_RETRIES),
            retry_backoff_base=_float_from_value(
                retry_backoff_base,
                default=DEFAULT_RETRY_BACKOFF_BASE,
            ),
            workers=_int_from_value(workers, default=DEFAULT_WORKERS),
        )


SETTINGS = Settings.from_env()
