import os
import stat

import pytest

from vigilo_connector.gateway.settings import ConfigError, load_settings


def _env(tmp_path, **extra):
    env = {"DATA_DIR": str(tmp_path)}
    env.update(extra)
    return env


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_passord_fra_miljo_vinner(tmp_path):
    s = load_settings(_env(tmp_path, ADMIN_PASSWORD="et-langt-nok-passord"))
    assert s.admin_password == "et-langt-nok-passord"
    assert s.password_generated is False
    assert not (tmp_path / "admin_password").exists()


def test_for_kort_passord_avvises(tmp_path):
    with pytest.raises(ConfigError, match="ADMIN_PASSWORD"):
        load_settings(_env(tmp_path, ADMIN_PASSWORD="kort"))


def test_passord_genereres_og_gjenbrukes(tmp_path):
    first = load_settings(_env(tmp_path))
    assert len(first.admin_password) == 64
    assert first.password_generated is True
    assert _mode(tmp_path / "admin_password") == 0o600

    second = load_settings(_env(tmp_path))
    assert second.admin_password == first.admin_password
    assert second.password_generated is False


def test_session_secret_persisteres(tmp_path):
    a = load_settings(_env(tmp_path))
    b = load_settings(_env(tmp_path))
    assert a.session_secret == b.session_secret
    assert len(a.session_secret) >= 32
    assert _mode(tmp_path / "session_secret") == 0o600


def test_api_token_valgfri_men_med_minstelengde(tmp_path):
    assert load_settings(_env(tmp_path)).api_token is None
    with pytest.raises(ConfigError, match="API_TOKEN"):
        load_settings(_env(tmp_path, API_TOKEN="for-kort"))
    tok = "x" * 32
    assert load_settings(_env(tmp_path, API_TOKEN=tok)).api_token == tok


def test_public_url_normaliseres(tmp_path):
    s = load_settings(_env(tmp_path, PUBLIC_URL="https://vigilo.example.ts.net/"))
    assert s.public_url == "https://vigilo.example.ts.net"
    assert load_settings(_env(tmp_path)).public_url is None


def test_public_url_maa_vaere_https(tmp_path):
    with pytest.raises(ConfigError, match="PUBLIC_URL"):
        load_settings(_env(tmp_path, PUBLIC_URL="http://vigilo.example"))


def test_standardverdier(tmp_path):
    s = load_settings(_env(tmp_path))
    assert s.port == 8000
    assert s.host == "127.0.0.1"
    assert str(s.tailscale_socket) == "/var/run/tailscale/tailscaled.sock"
    assert s.vigilo_dir == tmp_path / "vigilo"


def test_data_dir_maa_vaere_skrivbar(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        if os.access(ro, os.W_OK):  # f.eks. root i CI
            pytest.skip("kjører med skrivetilgang uansett")
        with pytest.raises(ConfigError, match="DATA_DIR"):
            load_settings({"DATA_DIR": str(ro)})
    finally:
        ro.chmod(0o700)
