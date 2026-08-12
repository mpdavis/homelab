"""Environment-driven configuration — everything is overridable without a rebuild."""

from __future__ import annotations

from pathlib import Path

import pytest

from ames_digest.config import Config

# Every variable Config reads, cleared before each test so the developer's own
# shell cannot change what the suite asserts.
ENV_VARS = [
    "AMES_WEBLINK_BASE_URL", "AMES_WEBLINK_REPO", "AMES_ROOT_FOLDER_ID", "AMES_BOARD",
    "AMES_LLM_BASE_URL", "AMES_LLM_API_KEY", "OPENCODE_API_KEY", "AMES_ITEM_MODEL",
    "AMES_DIGEST_MODEL", "AMES_ITEM_CHAR_BUDGET", "AMES_AGENDA_CHAR_BUDGET",
    "AMES_MINUTES_CHAR_BUDGET", "AMES_MAX_CONCURRENCY", "AMES_MEETING_TIME",
    "AMES_MEETING_LOCATION", "AMES_STATE_DIR", "AMES_OUTPUT_DIR", "AMES_DELIVERY",
    "AMES_PRICE_INPUT_PER_MTOK", "AMES_PRICE_OUTPUT_PER_MTOK",
    "NTFY_URL", "NTFY_TOPIC", "NTFY_TOKEN", "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME",
    "SMTP_PASSWORD", "SMTP_STARTTLS", "MAIL_FROM", "MAIL_TO",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class TestDefaults:
    def test_source_repository(self):
        cfg = Config()
        assert cfg.weblink_base_url == "https://publicdocs.cityofames.org/WebLink"
        assert cfg.weblink_repo == "COA"
        assert cfg.root_folder_id == 236500
        assert cfg.board == "City Council"

    def test_meeting_furniture(self):
        cfg = Config()
        assert cfg.meeting_time == "6:00 PM"
        assert cfg.meeting_location == "City Hall, 515 Clark Ave"

    def test_delivery_defaults_need_no_credentials(self):
        assert Config().delivery == ["file", "stdout"]

    def test_paths(self):
        cfg = Config()
        assert cfg.state_dir == Path("/data/state")
        assert cfg.output_dir == Path("/data/digests")

    def test_prices_are_unset(self):
        # A figure baked into the image would go stale while reading as
        # authoritative, so the spend estimate stays opt-in.
        cfg = Config()
        assert cfg.price_input_per_mtok is None
        assert not cfg.prices_configured


class TestOverrides:
    def test_strings(self, monkeypatch):
        monkeypatch.setenv("AMES_BOARD", "Parks and Recreation")
        assert Config().board == "Parks and Recreation"

    def test_blank_value_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("AMES_BOARD", "   ")
        assert Config().board == "City Council"

    def test_integers(self, monkeypatch):
        monkeypatch.setenv("AMES_MAX_CONCURRENCY", "8")
        assert Config().max_concurrency == 8

    def test_a_bad_integer_fails_loudly(self, monkeypatch):
        monkeypatch.setenv("AMES_MAX_CONCURRENCY", "lots")
        with pytest.raises(ValueError, match="must be an integer"):
            Config()

    def test_floats(self, monkeypatch):
        monkeypatch.setenv("AMES_PRICE_INPUT_PER_MTOK", "3.0")
        monkeypatch.setenv("AMES_PRICE_OUTPUT_PER_MTOK", "15.0")
        cfg = Config()
        assert cfg.prices_configured
        assert (cfg.price_input_per_mtok, cfg.price_output_per_mtok) == (3.0, 15.0)

    def test_one_price_alone_is_not_enough(self, monkeypatch):
        monkeypatch.setenv("AMES_PRICE_INPUT_PER_MTOK", "3.0")
        assert not Config().prices_configured

    def test_a_bad_float_fails_loudly(self, monkeypatch):
        monkeypatch.setenv("AMES_PRICE_INPUT_PER_MTOK", "free")
        with pytest.raises(ValueError, match="must be a number"):
            Config()

    def test_lists_are_split_and_stripped(self, monkeypatch):
        monkeypatch.setenv("AMES_DELIVERY", " file , ntfy ,, smtp ")
        assert Config().delivery == ["file", "ntfy", "smtp"]

    def test_paths_are_wrapped(self, monkeypatch):
        monkeypatch.setenv("AMES_STATE_DIR", "./state")
        assert Config().state_dir == Path("./state")

    def test_meeting_furniture_is_overridable(self, monkeypatch):
        monkeypatch.setenv("AMES_MEETING_TIME", "7:30 PM")
        monkeypatch.setenv("AMES_MEETING_LOCATION", "Council Chambers")
        cfg = Config()
        assert cfg.meeting_time == "7:30 PM"
        assert cfg.meeting_location == "Council Chambers"

    def test_starttls_is_on_unless_explicitly_disabled(self, monkeypatch):
        assert Config().smtp_starttls
        monkeypatch.setenv("SMTP_STARTTLS", "FALSE")
        assert not Config().smtp_starttls

    def test_mail_to_defaults_empty(self):
        assert Config().mail_to == []


class TestApiKey:
    def test_preferred_variable(self, monkeypatch):
        monkeypatch.setenv("AMES_LLM_API_KEY", "sk-primary")
        assert Config().llm_api_key == "sk-primary"

    def test_falls_back_to_the_opencode_variable(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_API_KEY", "sk-fallback")
        assert Config().llm_api_key == "sk-fallback"

    def test_primary_wins(self, monkeypatch):
        monkeypatch.setenv("AMES_LLM_API_KEY", "sk-primary")
        monkeypatch.setenv("OPENCODE_API_KEY", "sk-fallback")
        assert Config().llm_api_key == "sk-primary"

    def test_require_llm_raises_without_a_key(self):
        with pytest.raises(SystemExit, match="No model API key"):
            Config().require_llm()

    def test_require_llm_passes_with_a_key(self, monkeypatch):
        monkeypatch.setenv("AMES_LLM_API_KEY", "sk-x")
        Config().require_llm()
