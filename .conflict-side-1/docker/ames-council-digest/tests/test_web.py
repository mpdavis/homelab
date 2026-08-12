from ames_digest.record import MeetingRecord
from ames_digest import web


def test_public_pages_use_linked_styles_and_static_copy(tmp_path):
    web.publish(tmp_path)
    home = (tmp_path / "index.html").read_text()
    about = (tmp_path / "about.html").read_text()

    assert '<link rel="stylesheet" href="/styles.css">' in home
    assert "No meetings published yet." in home
    assert "Public records, made readable." in about
    assert (tmp_path / "styles.css").exists()


def test_meeting_page_renders_persisted_card_weights():
    stored = MeetingRecord(
        key="city-council-2026-07-28",
        board="City Council",
        meeting_date="2026-07-28",
        agenda={"meeting_time": "6:00 PM", "location": "City Hall"},
        items=[
            {"weight": "major", "title": "Water rates", "summary": "A rate change.", "why_it_matters": "Bills change.", "facts": [{"label": "Cost", "value": "$3"}]},
            {"weight": "routine", "title": "Claims", "summary": "Routine."},
        ],
        preview={"body": "## Notable Topics\nWater rates", "generated_at": "2026-07-28T18:00:00"},
    )
    page = web.render_meeting(stored)

    assert "Water rates" in page
    assert "Why it matters" in page
    assert "Claims" in page
    assert 'href="/styles.css"' in page
