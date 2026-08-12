"""The preview archive: what the outcome pass builds on.

Losing this file costs a re-summarized packet at real token expense, so the
read path is required to degrade rather than raise on anything it finds.
"""

from __future__ import annotations

import json

import pytest

from ames_digest import archive
from ames_digest.agenda import AgendaOutline
from ames_digest.llm import Usage

KEY = "city-council-2026-07-28"


def save(tmp_path, **overrides):
    payload = {
        "items": [{"entry_id": 7, "code": "A001", "summary": "s"}],
        "agenda": {"meeting_time": "6:00 PM", "location": "City Hall", "items": []},
        "body": "# page",
        "usage": Usage(100, 20, 5),
        "model": "claude-sonnet-4-5",
        "generated_at": "2026-07-28T18:00:00",
    }
    payload.update(overrides)
    archive.save_preview(tmp_path, KEY, payload.pop("items"), **payload)
    return archive.load_preview(tmp_path, KEY)


class TestRoundTrip:
    def test_everything_survives(self, tmp_path):
        loaded = save(tmp_path)
        assert loaded.items == [{"entry_id": 7, "code": "A001", "summary": "s"}]
        assert loaded.body == "# page"
        assert loaded.model == "claude-sonnet-4-5"
        assert loaded.generated_at == "2026-07-28T18:00:00"
        assert (loaded.usage.input_tokens, loaded.usage.calls) == (100, 5)

    def test_agenda_survives(self, tmp_path):
        outline = AgendaOutline.from_archive(save(tmp_path).agenda)
        assert outline.venue == "6:00 PM · City Hall"

    def test_writes_the_current_version(self, tmp_path):
        save(tmp_path)
        on_disk = json.loads((tmp_path / "meetings" / f"{KEY}.json").read_text())
        assert on_disk["version"] == archive.ARCHIVE_VERSION
        assert on_disk["key"] == KEY

    def test_creates_the_directory(self, tmp_path):
        save(tmp_path / "deep" / "nested")
        assert (tmp_path / "deep" / "nested" / "meetings" / f"{KEY}.json").exists()

    def test_overwrites_a_previous_archive(self, tmp_path):
        save(tmp_path)
        assert save(tmp_path, body="# second").body == "# second"

    def test_leaves_no_temporary_files(self, tmp_path):
        save(tmp_path)
        assert [p.name for p in (tmp_path / "meetings").iterdir()] == [f"{KEY}.json"]


class TestHasPage:
    @pytest.mark.parametrize("body, expected", [
        ("# page", True), ("", False), ("   \n ", False),
    ])
    def test(self, body, expected):
        assert archive.PreviewArchive(body=body).has_page is expected


class TestByEntryId:
    def test_keys_by_entry(self):
        arch = archive.PreviewArchive(items=[{"entry_id": 7, "code": "A001"},
                                             {"entry_id": 9, "code": "A002"}])
        assert set(arch.by_entry_id()) == {7, 9}

    @pytest.mark.parametrize("bad", [None, 0, "7", 1.5, True])
    def test_unusable_ids_are_dropped_not_collected_under_zero(self, bad):
        # `True` is an int in Python, which is exactly why this is worth pinning:
        # a truthy non-id must not become a key.
        arch = archive.PreviewArchive(items=[{"entry_id": bad}])
        assert all(isinstance(k, int) and k and not isinstance(k, bool)
                   for k in arch.by_entry_id())

    def test_missing_key(self):
        assert archive.PreviewArchive(items=[{"code": "A001"}]).by_entry_id() == {}


class TestLoadDegradesGracefully:
    def test_no_file_yields_an_empty_archive(self, tmp_path):
        loaded = archive.load_preview(tmp_path, KEY)
        assert loaded.items == [] and not loaded.has_page

    def test_corrupt_json(self, tmp_path):
        path = tmp_path / "meetings" / f"{KEY}.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        assert archive.load_preview(tmp_path, KEY).items == []

    def test_items_not_a_list(self, tmp_path):
        path = tmp_path / "meetings" / f"{KEY}.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"version": 4, "items": "nope", "body": "# page"}))
        loaded = archive.load_preview(tmp_path, KEY)
        assert loaded.items == []
        assert loaded.body == "# page", "a bad item list must not cost the page"

    def test_non_dict_items_are_filtered(self, tmp_path):
        path = tmp_path / "meetings" / f"{KEY}.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"version": 4, "items": [None, 3, {"entry_id": 1}]}))
        assert archive.load_preview(tmp_path, KEY).items == [{"entry_id": 1}]

    def test_body_of_the_wrong_type(self, tmp_path):
        path = tmp_path / "meetings" / f"{KEY}.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"version": 4, "items": [], "body": 42}))
        assert archive.load_preview(tmp_path, KEY).body == ""

    @pytest.mark.parametrize("agenda", [None, "nope", 42, []])
    def test_agenda_of_the_wrong_type(self, tmp_path, agenda):
        path = tmp_path / "meetings" / f"{KEY}.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"version": 4, "items": [], "agenda": agenda}))
        assert archive.load_preview(tmp_path, KEY).agenda == {}

    def test_a_v1_archive_still_yields_its_items(self, tmp_path):
        # No page body, so the caller falls back to writing from the minutes.
        path = tmp_path / "meetings" / f"{KEY}.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"version": 1, "items": [{"entry_id": 7}]}))
        loaded = archive.load_preview(tmp_path, KEY)
        assert loaded.items == [{"entry_id": 7}]
        assert not loaded.has_page

    def test_missing_usage_block(self, tmp_path):
        path = tmp_path / "meetings" / f"{KEY}.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"version": 4, "items": []}))
        assert archive.load_preview(tmp_path, KEY).usage == Usage()


class TestUnsafeKeys:
    @pytest.mark.parametrize("key", [
        "../escape", "/etc/passwd", "city council", "City-Council", "", ".hidden",
        "a/b", "-leading-dash",
    ])
    def test_save_refuses(self, tmp_path, key):
        with pytest.raises(ValueError, match="unsafe meeting key"):
            archive.save_preview(tmp_path, key, [], agenda={}, body="", usage=Usage(),
                                 model="m", generated_at="")

    @pytest.mark.parametrize("key", ["../escape", "city council", ""])
    def test_load_returns_empty_rather_than_raising(self, tmp_path, key):
        assert archive.load_preview(tmp_path, key).items == []

    @pytest.mark.parametrize("key", [
        "city-council-2026-07-28", "city-council-2026-03-24-tax-levy", "a", "a1",
    ])
    def test_real_meeting_keys_are_accepted(self, tmp_path, key):
        archive.save_preview(tmp_path, key, [], agenda={}, body="x", usage=Usage(),
                             model="m", generated_at="")
        assert archive.load_preview(tmp_path, key).body == "x"


class TestRevisionHistory:
    def test_revision_round_trips(self, tmp_path):
        assert save(tmp_path, revision=3).revision == 3

    def test_defaults_to_zero(self, tmp_path):
        assert save(tmp_path).revision == 0

    @pytest.mark.parametrize("bad", [None, "3", True, 1.5])
    def test_a_malformed_revision_reads_as_zero(self, tmp_path, bad):
        path = tmp_path / "meetings" / f"{KEY}.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"version": 4, "items": [], "revision": bad}))
        assert archive.load_preview(tmp_path, KEY).revision == 0

    def test_usage_is_the_pages_cumulative_total(self, tmp_path):
        # The next pass reads its prior_usage from here, so a revision that
        # stored only its own spend would erase the original from the footer.
        loaded = save(tmp_path, usage=Usage(300_000, 40_000, 45), revision=1)
        assert loaded.usage.input_tokens == 300_000
        assert loaded.usage.calls == 45
