"""Where a rendered digest goes, and what happens when one sink fails."""

from __future__ import annotations

import pytest

from ames_digest import index
from ames_digest.config import Config
from ames_digest.delivery import (
    DeliveryError,
    FileSink,
    StdoutSink,
    build_sinks,
    deliver_all,
    gateway_prices,
)
from ames_digest.render import RenderedDigest
from ames_digest.record import MeetingRecord


@pytest.fixture
def rendered():
    return RenderedDigest(
        subject="City Council — July 28, 2026",
        markdown="# City Council — July 28, 2026\n\nBody.",
        html="<html><title>City Council — July 28, 2026</title></html>",
        text="City Council",
        filename_stem="city-council-2026-07-28",
        record=MeetingRecord(key="city-council-2026-07-28", board="City Council", meeting_date="2026-07-28"),
    )


@pytest.fixture
def cfg(tmp_path):
    config = Config()
    config.output_dir = tmp_path / "digests"
    config.state_dir = tmp_path / "state"
    config.price_input_per_mtok = None
    config.price_output_per_mtok = None
    return config


class TestGatewayPrices:
    def test_none_unless_both_are_set(self, cfg):
        assert gateway_prices(cfg) is None
        cfg.price_input_per_mtok = 3.0
        assert gateway_prices(cfg) is None

    def test_both_set(self, cfg):
        cfg.price_input_per_mtok, cfg.price_output_per_mtok = 3.0, 15.0
        assert gateway_prices(cfg) == (3.0, 15.0)


class TestBuildSinks:
    def test_defaults(self, cfg):
        assert [s.name for s in build_sinks(cfg)] == ["file", "stdout"]

    def test_unknown_sink_is_rejected_with_the_valid_names(self, cfg):
        cfg.delivery = ["file", "carrier-pigeon"]
        with pytest.raises(DeliveryError, match="carrier-pigeon"):
            build_sinks(cfg)

    def test_empty_delivery_list(self, cfg):
        cfg.delivery = []
        assert build_sinks(cfg) == []


class TestFileSink:
    def test_writes_markdown_and_html(self, cfg, rendered):
        FileSink().deliver(rendered, cfg)
        assert (cfg.output_dir / "city-council-2026-07-28.md").read_text().startswith("#")
        assert (cfg.output_dir / "city-council-2026-07-28.html").exists()
        assert (cfg.output_dir / "city-council-2026-07-28.json").exists()

    def test_creates_the_output_directory(self, cfg, rendered):
        assert not cfg.output_dir.exists()
        FileSink().deliver(rendered, cfg)
        assert cfg.output_dir.is_dir()

    def test_refreshes_the_index(self, cfg, rendered):
        FileSink().deliver(rendered, cfg)
        assert (cfg.output_dir / index.INDEX_FILENAME).exists()

    def test_the_outcome_pass_overwrites_both_files(self, cfg, rendered):
        FileSink().deliver(rendered, cfg)
        updated = RenderedDigest(**{**rendered.__dict__, "markdown": "# Updated"})
        FileSink().deliver(updated, cfg)
        assert (cfg.output_dir / "city-council-2026-07-28.md").read_text() == "# Updated"

    def test_reports_where_it_wrote(self, cfg, rendered):
        assert "city-council-2026-07-28" in FileSink().deliver(rendered, cfg)


class TestStdoutSink:
    def test_prints_the_markdown(self, cfg, rendered, capsys):
        result = StdoutSink().deliver(rendered, cfg)
        assert "# City Council — July 28, 2026" in capsys.readouterr().out
        assert result == "printed to stdout"


class Boom:
    name = "boom"

    def deliver(self, rendered, cfg):
        raise RuntimeError("nope")


class Fine:
    name = "fine"

    def deliver(self, rendered, cfg):
        return "delivered"


class TestDeliverAll:
    def test_reports_each_sink(self, cfg, rendered):
        assert deliver_all(rendered, cfg, [Fine()]) == ["fine: delivered"]

    def test_one_failure_does_not_cost_the_others(self, cfg, rendered):
        results = deliver_all(rendered, cfg, [Fine(), Boom()])
        assert "fine: delivered" in results
        assert any(r.startswith("FAILED boom") for r in results)

    def test_a_digest_that_reached_nobody_is_a_failed_run(self, cfg, rendered):
        with pytest.raises(DeliveryError, match="nope"):
            deliver_all(rendered, cfg, [Boom()])

    def test_no_sinks_at_all_is_not_an_error(self, cfg, rendered):
        # Nothing failed; there was simply nothing configured to do.
        assert deliver_all(rendered, cfg, []) == []
