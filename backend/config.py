from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

APP_VERSION = "0.9.0"

REPO_ROOT = Path(__file__).resolve().parents[1]
_AI_STATUS_CACHE: dict[str, object] = {"expires_at": 0.0, "status": None}


def _path_from_env(name: str, default: str) -> Path:
    raw = os.environ.get(name, default)
    path = Path(raw)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def _env(name: str, default: str = "") -> str:
    """Read ``SERIN_<name>``, falling back to the legacy ``FINCH_<name>`` so
    pre-rename ``.env`` files keep working. Returns ``default`` if neither set."""
    val = os.environ.get(f"SERIN_{name}")
    if val is None:
        val = os.environ.get(f"FINCH_{name}")
    return default if val is None else val


def _resolve_db_path() -> Path:
    raw = _env("DB_PATH")
    if raw:
        path = Path(raw)
        if not path.is_absolute():
            path = REPO_ROOT / path
    else:
        path = REPO_ROOT / "data" / "serin.db"
    # Data-safety across the Finch→Serin rename: if the chosen DB doesn't exist
    # yet but a pre-rename finch.db sits beside it, keep using that file so no
    # portfolio is orphaned. Fresh installs get serin.db.
    if not path.exists():
        legacy = path.with_name("finch.db")
        if legacy.exists():
            return legacy
    return path


@dataclass
class Settings:
    app_name: str = "Serin"
    backend_host: str = _env("BACKEND_HOST", "127.0.0.1")
    backend_port: int = int(_env("BACKEND_PORT", "8890"))
    frontend_port: int = int(_env("FRONTEND_PORT", "5174"))
    db_path: Path = _resolve_db_path()
    database_url: str = os.environ.get("DATABASE_URL", "")
    # "auto", not "fmp": unset + no key resolves "fmp" to "none", so a fresh
    # install had no prices at all. See resolved_market_data_provider, which
    # has always documented auto as the default — set "fmp" explicitly to get
    # the strict, error-if-unconfigured behaviour back.
    market_data_provider: str = _env("MARKET_DATA_PROVIDER", "auto")
    fmp_api_key: str = os.environ.get("FMP_API_KEY", "")
    fmp_base_url: str = os.environ.get("FMP_BASE_URL", "https://financialmodelingprep.com")
    ai_provider: str = _env("AI_PROVIDER", "auto")
    claude_code_oauth_token: str = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    # Lets a Pro/cloud key-proxy or a future mobile client point briefings at
    # an alternative endpoint without forking the briefings module.
    anthropic_base_url: str = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    deepseek_api_key: str = os.environ.get("DEEPSEEK_API_KEY", "")
    deepseek_model: str = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
    deepseek_base_url: str = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    snaptrade_client_id: str = os.environ.get("SNAPTRADE_CLIENT_ID", "")
    snaptrade_consumer_key: str = os.environ.get("SNAPTRADE_CONSUMER_KEY", "")
    # Personal-tier keys ship with a pre-provisioned user (registerUser is
    # blocked). Standard keys leave these blank and register users via the API.
    snaptrade_user_id: str = os.environ.get("SNAPTRADE_USER_ID", "")
    snaptrade_user_secret: str = os.environ.get("SNAPTRADE_USER_SECRET", "")
    # App lock: when set, /api/* requires the passphrase (web login) or the
    # derived bearer token (mobile). Empty = open, the localhost default.
    auth_password: str = _env("AUTH_PASSWORD", "")
    # Directory of out-of-tree plugins loaded at startup (open-core seam;
    # see backend/plugins.py). Empty = no external plugins.
    plugins_dir: str = _env("PLUGINS_DIR", "")
    # Serin billing origin — where a license key redeems the Intelligence pack
    # download (POST /api/admin/install-pack). Empty until the user deploys it.
    billing_url: str = _env("BILLING_URL", "")
    # Set on managed Serin Cloud tenants (Dockerfile ENV). Surfaces the
    # "provided by Serin Cloud" badge on env-supplied operator keys; never set
    # on self-host.
    cloud_managed: bool = _env("CLOUD", "").strip() in ("1", "true", "yes")
    # Log format: "text" (default) or "json" for structured request logs.
    log_format: str = _env("LOG_FORMAT", "text")
    smtp_host: str = _env("SMTP_HOST", "")
    smtp_port: int = int(_env("SMTP_PORT", "587"))
    smtp_username: str = _env("SMTP_USERNAME", "")
    smtp_password: str = _env("SMTP_PASSWORD", "")
    smtp_tls: str = _env("SMTP_TLS", "auto")  # auto | starttls | ssl | none
    email_from: str = _env("EMAIL_FROM", "")
    email_to: str = _env("EMAIL_TO", "")

    @property
    def claude_cli_available(self) -> bool:
        return shutil.which("claude") is not None

    @property
    def claude_cli_configured(self) -> bool:
        return self.claude_cli_available

    @property
    def anthropic_configured(self) -> bool:
        return bool(self.anthropic_api_key.strip())

    @property
    def deepseek_configured(self) -> bool:
        return bool(self.deepseek_api_key.strip())

    @property
    def fmp_configured(self) -> bool:
        return bool(self.fmp_api_key.strip())

    @property
    def resolved_market_data_provider(self) -> str:
        """Resolve which market-data provider to use.

        - Explicit ``fmp`` honours the request even if unconfigured (so the
          user sees a clear error path).
        - Explicit ``yahoo`` always works — it needs no key.
        - ``auto`` (default) prefers FMP when configured (paid, richer
          fundamentals) and falls back to Yahoo (free, no key) so Finch is
          usable out-of-the-box — the same baseline Ghostfolio gives.
        """
        requested = self.market_data_provider.strip().lower()
        if requested == "yahoo":
            return "yahoo"
        if requested == "fmp":
            return "fmp" if self.fmp_configured else "none"
        if requested == "auto":
            return "fmp" if self.fmp_configured else "yahoo"
        return "none"

    @property
    def database_engine(self) -> str:
        return "sqlite"

    @property
    def database_url_configured(self) -> bool:
        return bool(self.database_url.strip())

    @property
    def resolved_ai_provider(self) -> str:
        """Resolve which AI provider to use.

        Explicit FINCH_AI_PROVIDER always wins. In "auto" mode, API-key
        providers are preferred over the Claude CLI: the CLI rides a personal
        Claude subscription login, which is a local development convenience
        only and must not power a deployed/production instance.
        """
        requested = self.ai_provider.strip().lower()
        if requested == "claude_cli":
            return "claude_cli" if self.claude_cli_configured else "none"
        if requested == "anthropic_api":
            return "anthropic_api" if self.anthropic_configured else "none"
        if requested == "deepseek":
            return "deepseek" if self.deepseek_configured else "none"
        if self.anthropic_configured:
            return "anthropic_api"
        if self.deepseek_configured:
            return "deepseek"
        if self.claude_cli_configured:
            return "claude_cli"
        return "none"

    @property
    def ai_model(self) -> str:
        if self.resolved_ai_provider == "deepseek":
            return self.deepseek_model
        return self.anthropic_model

    @property
    def ai_configured(self) -> bool:
        return self.resolved_ai_provider != "none"

    @property
    def resolved_email_from(self) -> str:
        return self.email_from.strip() or self.smtp_username.strip()

    @property
    def email_configured(self) -> bool:
        return bool(self.smtp_host.strip() and self.email_to.strip() and self.resolved_email_from)

    @property
    def snaptrade_configured(self) -> bool:
        return bool(self.snaptrade_client_id.strip() and self.snaptrade_consumer_key.strip())


settings = Settings()


def _friendly_cli_status_error(detail: str) -> str:
    if (
        "authentication_error" in detail
        or "Invalid authentication credentials" in detail
        or "Failed to authenticate" in detail
    ):
        return (
            "Claude CLI authentication failed. Run claude auth login --claudeai "
            "to refresh your Claude subscription login. For a long-lived local "
            "Finch token, run claude setup-token and set CLAUDE_CODE_OAUTH_TOKEN "
            "in .env, then restart Finch."
        )
    return detail or "Claude CLI did not return a usable response."


_ANTHROPIC_DEFAULT_BASE = "https://api.anthropic.com"


def ai_is_managed() -> bool:
    """Whether Anthropic-path AI calls are being served by Serin's managed
    proxy rather than the user's own key.

    The only thing that rewrites ``anthropic_base_url`` is the pack's
    managed-AI activation (it points the base at the metering proxy and uses
    the license token as the key), so a non-default base *is* the signal —
    no pack import needed on this side of the seam.
    """
    return settings.anthropic_base_url.rstrip("/") != _ANTHROPIC_DEFAULT_BASE


def get_ai_status(force: bool = False) -> dict[str, object]:
    now = time.time()
    if not force and _AI_STATUS_CACHE["status"] and now < float(_AI_STATUS_CACHE["expires_at"]):
        return dict(_AI_STATUS_CACHE["status"])  # type: ignore[arg-type]

    # Lazy import: keeps config importable before the connector registry loads
    # and avoids the config <-> connectors circular import. Portal-stored keys
    # (AI briefing connector config) must count as "configured" here.
    from backend.ai_provider import anthropic_available, deepseek_available, resolved_provider

    requested_provider = settings.ai_provider.strip().lower()
    provider = resolved_provider()
    known = {"claude_cli", "anthropic_api", "deepseek"}
    status_provider = requested_provider if requested_provider in known else provider
    status: dict[str, object] = {
        "provider": status_provider,
        "model": settings.ai_model,
        "configured": provider != "none",
        "ready": provider != "none",
        "managed": False,
        "error": "",
    }

    if provider == "none" and requested_provider == "anthropic_api":
        status.update({"ready": False, "error": "Anthropic API key is not configured (AI briefing connector or ANTHROPIC_API_KEY)."})
    elif provider == "none" and requested_provider == "deepseek":
        status.update({"ready": False, "error": "DeepSeek API key is not configured (AI briefing connector or DEEPSEEK_API_KEY)."})
    elif provider == "none" and requested_provider == "claude_cli":
        status.update({"ready": False, "error": "Claude CLI is not installed, not in PATH, or not authenticated."})
    elif provider == "none":
        status.update({"ready": False, "error": "No AI provider configured. Add a key in the AI briefing connector, or set ANTHROPIC_API_KEY / DEEPSEEK_API_KEY."})
    elif provider == "anthropic_api":
        status.update({"ready": anthropic_available(), "model": settings.anthropic_model})
        if not anthropic_available():
            status["error"] = "Anthropic API key is not configured (AI briefing connector or ANTHROPIC_API_KEY)."
        elif ai_is_managed():
            # Managed AI: the pack routed this through Serin's metering proxy.
            # Which model serves it is our implementation detail, not the
            # customer's configuration — naming it invites treating it as a
            # promise.
            status.update({"managed": True, "model": ""})
    elif provider == "deepseek":
        status.update({"ready": deepseek_available(), "model": settings.deepseek_model})
        if not deepseek_available():
            status["error"] = "DeepSeek API key is not configured (AI briefing connector or DEEPSEEK_API_KEY)."
    elif provider == "claude_cli":
        claude_bin = shutil.which("claude")
        if not claude_bin:
            status.update({"ready": False, "error": "Claude CLI is not installed or not in PATH."})
        else:
            env = os.environ.copy()
            if settings.claude_code_oauth_token:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token
            try:
                result = subprocess.run(
                    [claude_bin, "--print", "--model", settings.anthropic_model, "Reply with OK only."],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=env,
                )
            except Exception as exc:
                status.update({"ready": False, "error": f"Claude CLI health check failed: {exc}"})
            else:
                detail = (result.stderr or result.stdout or "").strip()[:800]
                if result.returncode != 0:
                    status.update({"ready": False, "error": _friendly_cli_status_error(detail)})
                elif not (result.stdout or "").strip():
                    status.update({"ready": False, "error": "Claude CLI response was empty."})

    _AI_STATUS_CACHE["status"] = dict(status)
    _AI_STATUS_CACHE["expires_at"] = now + (300 if status["ready"] else 10)
    return status
