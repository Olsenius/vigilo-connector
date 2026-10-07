"""Konfigurasjon for gatewayen, lest fra miljøvariabler.

Passord og økt-nøkkel genereres ved første oppstart hvis de ikke er satt, og
lagres i datavolumet (chmod 600) så de overlever omstart og nye images.
"""

import os
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

MIN_PASSWORD_LEN = 12
MIN_API_TOKEN_LEN = 32
DEFAULT_TAILSCALE_SOCKET = "/var/run/tailscale/tailscaled.sock"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    public_url: str | None
    admin_password: str
    password_generated: bool
    api_token: str | None
    session_secret: bytes
    host: str
    port: int
    tailscale_socket: Path
    log_level: str

    @property
    def vigilo_dir(self) -> Path:
        return self.data_dir / "vigilo"

    @property
    def oauth_db(self) -> Path:
        return self.data_dir / "oauth.db"

    @property
    def password_file(self) -> Path:
        return self.data_dir / "admin_password"


def read_or_create_secret(path: Path, factory: Callable[[], str]) -> tuple[str, bool]:
    """Les en hemmelighet fra fil, eller lag og lagre en ny. Returnerer (verdi, ny?)."""
    if path.exists():
        return path.read_text().strip(), False
    value = factory()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value + "\n")
    return value, True


def _check_data_dir(data_dir: Path) -> None:
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ConfigError(f"DATA_DIR {data_dir} kan ikke opprettes: {e}") from e
    if not os.access(data_dir, os.W_OK):
        raise ConfigError(f"DATA_DIR {data_dir} er ikke skrivbar for gatewayen.")


def _public_url(raw: str) -> str | None:
    raw = raw.strip().rstrip("/")
    if not raw:
        return None
    if not raw.startswith("https://"):
        raise ConfigError(f"PUBLIC_URL må starte med https:// (fikk {raw!r}).")
    return raw


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    data_dir = Path(env.get("DATA_DIR", "/data"))
    _check_data_dir(data_dir)

    password = env.get("ADMIN_PASSWORD", "")
    generated = False
    if password:
        if len(password) < MIN_PASSWORD_LEN:
            raise ConfigError(f"ADMIN_PASSWORD må være minst {MIN_PASSWORD_LEN} tegn.")
    else:
        password, generated = read_or_create_secret(
            data_dir / "admin_password", lambda: secrets.token_urlsafe(48)
        )

    api_token = env.get("API_TOKEN", "") or None
    if api_token and len(api_token) < MIN_API_TOKEN_LEN:
        raise ConfigError(f"API_TOKEN må være minst {MIN_API_TOKEN_LEN} tegn (bruk f.eks. `openssl rand -hex 32`).")

    session_secret, _ = read_or_create_secret(
        data_dir / "session_secret", lambda: secrets.token_urlsafe(32)
    )

    try:
        port = int(env.get("PORT", "8000"))
    except ValueError as e:
        raise ConfigError(f"PORT må være et tall (fikk {env.get('PORT')!r}).") from e

    return Settings(
        data_dir=data_dir,
        public_url=_public_url(env.get("PUBLIC_URL", "")),
        admin_password=password,
        password_generated=generated,
        api_token=api_token,
        session_secret=session_secret.encode(),
        host=env.get("HOST", "127.0.0.1"),
        port=port,
        tailscale_socket=Path(env.get("TAILSCALE_SOCKET", DEFAULT_TAILSCALE_SOCKET)),
        log_level=env.get("LOG_LEVEL", "INFO").upper(),
    )
