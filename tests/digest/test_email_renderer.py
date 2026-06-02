from digest.email_renderer import EmailRenderer, RenderContext
from digest.profile_builder import AttendeeProfile


def _profile(name="Sarah Smith", org="NJJ", qa=None, blurb=None):
    first = name.split()[0].lower() if name else "x"
    return AttendeeProfile(
        eb_attendee_id="x",
        name=name,
        email=f"{first}@x.com",
        org=org,
        role="Editor",
        blurb=blurb if blurb is not None else f"{name} — {org}, Editor.",
        form_qa=qa or [],
        is_known_ccm_contact=False,
        crm_contact_id=None,
        created_at="2026-05-01T10:00:00Z",
    )


def _ctx(**overrides):
    base = dict(
        event_title="x",
        event_when="x",
        event_location="x",
        total_count=1,
        new_attendees=[],
        existing_attendees=[],
        subject="x",
        logo_url=None,
    )
    base.update(overrides)
    return RenderContext(**base)


def test_render_includes_event_title_and_when():
    r = EmailRenderer()
    html = r.render(
        _ctx(
            event_title="AI in the newsroom",
            event_when="Friday, March 14, 2026 at 1:00 PM ET",
            event_location="Zoom",
            new_attendees=[_profile()],
        )
    )
    assert "AI in the newsroom" in html
    assert "Friday, March 14, 2026 at 1:00 PM ET" in html
    assert "Zoom" in html


def test_render_shows_attendee_name_and_qa_in_table():
    """The at-a-glance table renders each attendee's name plus a column per
    registration question, with their answer in the cell."""
    r = EmailRenderer()
    html = r.render(
        _ctx(
            new_attendees=[
                _profile(qa=[{"question": "What do you hope to learn?", "answer": "AI ethics"}])
            ]
        )
    )
    assert "Sarah Smith" in html
    assert "What do you hope to learn?" in html  # column header
    assert "AI ethics" in html  # answer cell
    assert "<table" in html
    assert "<td" in html


def test_render_table_columns_are_union_of_questions():
    """Columns adapt to whatever questions the event's form collected — the
    union across attendees, in first-seen order, not a hardcoded set."""
    r = EmailRenderer()
    a = _profile(name="Ann A", qa=[{"question": "Session?", "answer": "Morning"}])
    b = _profile(
        name="Bob B",
        qa=[
            {"question": "Session?", "answer": "Afternoon"},
            {"question": "Topics?", "answer": "FOIA"},
        ],
    )
    html = r.render(_ctx(total_count=2, new_attendees=[a, b]))
    assert "Session?" in html
    assert "Topics?" in html
    # Bob answered Topics; Ann didn't — her Topics cell is the em-dash placeholder.
    assert "FOIA" in html


def test_render_duplicate_question_labels_keep_distinct_columns():
    """Two registration questions can share the same prompt text but have
    different question_ids. Keying columns on the display text alone would
    collapse them into one column and silently drop one answer. Key on the
    stable question_id so both answers survive into the briefing."""
    r = EmailRenderer()
    p = AttendeeProfile(
        eb_attendee_id="x",
        name="Dana D",
        email="dana@x.com",
        org="Org",
        role="Editor",
        blurb="Dana D — Org, Editor.",
        form_qa=[
            {"question_id": "q1", "question": "Your goals?", "answer": "Learn FOIA"},
            {"question_id": "q2", "question": "Your goals?", "answer": "Meet peers"},
        ],
        is_known_ccm_contact=False,
        crm_contact_id=None,
        created_at="2026-05-01T10:00:00Z",
    )
    html = r.render(_ctx(new_attendees=[p]))
    # Both answers must appear — neither is silently dropped.
    assert "Learn FOIA" in html
    assert "Meet peers" in html
    # Two distinct data columns (plus the Attendee column) -> 3 header cells.
    # Match "<th " (trailing space) so the <thead> tag isn't miscounted.
    assert html.count("<th ") == 3


def test_render_initial_briefing_omits_new_badge():
    """On the initial briefing (is_initial=True) every attendee is new, so a
    per-row 'new' badge would be uniform noise. Suppression keys on the explicit
    is_initial signal, not on whether existing rows happen to be present."""
    r = EmailRenderer()
    profiles = [_profile(name="Ann A"), _profile(name="Bob B")]
    html = r.render(_ctx(total_count=2, new_attendees=profiles, existing_attendees=[], is_initial=True))
    assert "Ann A" in html
    assert "Bob B" in html
    assert "&middot; new" not in html  # initial briefing -> no per-row badge
    assert "since the last update" not in html  # nor the daily "N new" copy


def test_render_daily_digest_badges_new_rows_without_existing_rows():
    """A daily digest (is_initial=False) with only new attendees and no existing
    rows must still flag them as new. The earlier proxy (badge only when existing
    rows present) mislabeled the first daily after an empty-cursor initial as a
    plain roster — this pins the corrected, signal-driven behavior."""
    r = EmailRenderer()
    profiles = [_profile(name="Ann A"), _profile(name="Bob B")]
    html = r.render(
        _ctx(total_count=2, new_attendees=profiles, existing_attendees=[], is_initial=False)
    )
    assert "&middot; new" in html
    assert "since the last update" in html


def test_render_whitespace_only_answer_shows_placeholder():
    """A present-but-whitespace answer must collapse to the em-dash placeholder,
    not a visually-blank cell that's indistinguishable from a dropped value."""
    r = EmailRenderer()
    p = _profile(qa=[{"question": "Goals?", "answer": "   \n  "}])
    html = r.render(_ctx(new_attendees=[p]))
    assert "Goals?" in html  # column still renders
    assert ">—</td>" in html  # the answer cell is the placeholder


def test_render_includes_sheet_button_only_when_url_present():
    r = EmailRenderer()
    url = "https://docs.google.com/spreadsheets/d/abc/edit"
    with_url = r.render(_ctx(new_attendees=[_profile()], sheet_url=url))
    assert url in with_url
    assert "View full attendee sheet" in with_url

    without = r.render(_ctx(new_attendees=[_profile()]))
    assert "View full attendee sheet" not in without


def test_render_rejects_dangerous_sheet_url_scheme():
    """sheet_url comes from an editable Airtable field, so a javascript:/data:/
    plain-http value must never become a clickable button. Bad scheme -> the
    button (and its intro clause) are omitted entirely, fail-safe."""
    r = EmailRenderer()
    for bad in ["javascript:alert(1)", "data:text/html,x", "http://docs.google.com/x"]:
        html = r.render(_ctx(new_attendees=[_profile()], sheet_url=bad))
        assert "View full attendee sheet" not in html
        assert bad not in html
        assert "javascript:" not in html


def test_render_rejects_non_google_sheet_host():
    """We only ever generate Google Sheets, so an off-host https URL (a
    phishing link slipped into the field) is rejected rather than rendered."""
    r = EmailRenderer()
    html = r.render(_ctx(new_attendees=[_profile()], sheet_url="https://evil.example/phish"))
    assert "View full attendee sheet" not in html
    assert "evil.example" not in html


def test_render_rejects_google_redirector_and_non_sheets_pages():
    """A google.com hostname alone isn't enough: the open redirector
    (google.com/url?q=) and non-Sheets Google pages (Docs, Sites) must not
    render as the canonical 'full attendee sheet' button. Only the
    docs.google.com/spreadsheets/ shape we actually generate is accepted."""
    r = EmailRenderer()
    for bad in [
        "https://www.google.com/url?q=https://evil.example",
        "https://docs.google.com/document/d/abc/edit",
        "https://sites.google.com/view/phish",
    ]:
        html = r.render(_ctx(new_attendees=[_profile()], sheet_url=bad))
        assert "View full attendee sheet" not in html


def test_render_rejects_sheet_url_with_embedded_control_chars():
    """A newline can smuggle a second URL past urlparse, which strips control
    chars before validating the host. Reject embedded control characters so the
    off-host URL never reaches the HTML href or the plain-text alternative."""
    r = EmailRenderer()
    bad = "https://docs.google.com/spreadsheets/d/abc\nhttps://evil.example/phish"
    html = r.render(_ctx(new_attendees=[_profile()], sheet_url=bad))
    assert "View full attendee sheet" not in html
    assert "evil.example" not in html
    text = r.render_plain_text(_ctx(new_attendees=[_profile()], sheet_url=bad))
    assert "evil.example" not in text


def test_render_rejects_sheet_url_path_traversal():
    """A /spreadsheets/../document/ path resolves off the Sheets prefix at
    navigation time; the strict /spreadsheets/d/ pattern rejects it."""
    r = EmailRenderer()
    bad = "https://docs.google.com/spreadsheets/../document/d/abc/edit"
    html = r.render(_ctx(new_attendees=[_profile()], sheet_url=bad))
    assert "View full attendee sheet" not in html


def test_render_rejects_sheet_url_dot_segment_after_valid_prefix():
    """A '..' segment AFTER a valid /spreadsheets/d/<id> start still matches the
    prefix pattern, but the browser normalizes it off the Sheets area at
    navigation time, so the button could point at any docs.google.com page.
    Any '..' path segment is rejected regardless of where it appears."""
    r = EmailRenderer()
    bad = "https://docs.google.com/spreadsheets/d/abc/../../../document/d/XYZ/edit"
    html = r.render(_ctx(new_attendees=[_profile()], sheet_url=bad))
    assert "View full attendee sheet" not in html
    assert "document/d/XYZ" not in html


def test_render_unnamed_attendee_shows_placeholder():
    """Eventbrite can omit a name entirely; the attendee cell must show
    '(unnamed)' rather than a blank, identifier-less row."""
    r = EmailRenderer()
    p = _profile(name="", qa=[{"question": "Goals?", "answer": "x"}])
    html = r.render(_ctx(new_attendees=[p]))
    assert "(unnamed)" in html


def test_render_omits_topics_copy_when_no_question_columns():
    """With no registration questions the table is name-only, so the intro must
    not promise 'the topics they want you to cover'."""
    r = EmailRenderer()
    html = r.render(_ctx(new_attendees=[_profile(qa=[])]))
    assert "Sarah Smith" in html
    assert "topics they want you to cover" not in html


def test_render_plain_text_preserves_sheet_url():
    """Text-only clients must still get the sheet link. The plain-text builder
    emits it on its own line so the URL survives, not just a button label."""
    r = EmailRenderer()
    url = "https://docs.google.com/spreadsheets/d/ABC/edit"
    text = r.render_plain_text(_ctx(new_attendees=[_profile()], sheet_url=url))
    assert url in text
    assert "View full attendee sheet" in text


def test_render_plain_text_is_legible_per_attendee():
    """The plain-text alternative is built from structured data, not stripped
    from the HTML table, so each attendee's answers stay attached to a labeled
    line instead of running together."""
    r = EmailRenderer()
    a = _profile(name="Ann A", qa=[{"question": "Session?", "answer": "Morning"}])
    b = _profile(name="Bob B", qa=[{"question": "Session?", "answer": "Afternoon"}])
    text = r.render_plain_text(_ctx(total_count=2, new_attendees=[a, b]))
    lines = text.splitlines()
    # Each attendee starts its own line (a daily digest appends " (new)"), each
    # answer on a labeled line beneath.
    assert any(line.startswith("Ann A") for line in lines)
    assert any(line.strip() == "Session?: Morning" for line in lines)
    assert any(line.strip() == "Session?: Afternoon" for line in lines)
    assert "<" not in text and ">" not in text


def test_render_drops_dead_admin_manage_link():
    """The old 'Manage this digest' admin link pointed at an unbuilt backend.
    It must not reappear; the footer offers a reply-to instead."""
    r = EmailRenderer()
    html = r.render(_ctx(new_attendees=[_profile()]))
    assert "Manage this digest" not in html
    assert "/admin" not in html
    assert "reply to this email" in html.lower()


def test_render_includes_logo_when_logo_url_set():
    """A non-empty logo_url renders the CCM logo <img> in the email header."""
    r = EmailRenderer()
    html = r.render(_ctx(logo_url="https://example.org/ccm-logo.png"))
    assert '<img src="https://example.org/ccm-logo.png"' in html
    assert 'alt="Center for Cooperative Media"' in html


def test_render_omits_logo_when_logo_url_none():
    """logo_url=None leaves the {% if logo_url %} block unrendered — no <img>."""
    r = EmailRenderer()
    html = r.render(_ctx(logo_url=None))
    assert "<img" not in html


def test_render_or_none_returns_none_when_no_attendees():
    r = EmailRenderer()
    out = r.render_or_none(_ctx(total_count=0))
    assert out is None


def test_render_or_none_returns_html_when_existing_only():
    r = EmailRenderer()
    out = r.render_or_none(_ctx(total_count=1, existing_attendees=[_profile()]))
    assert out is not None
    assert "Sarah Smith" in out


def test_plain_text_alternative_strips_tags():
    r = EmailRenderer()
    text = r.render_plain_text(
        _ctx(
            event_title="AI in the newsroom",
            event_when="Friday, March 14, 2026 at 1:00 PM ET",
            new_attendees=[_profile()],
        )
    )
    assert "AI in the newsroom" in text
    assert "<" not in text
    assert ">" not in text


def test_render_escapes_html_in_attendee_data():
    """XSS guard: malicious attendee data must render as escaped text, not live HTML.
    Webmail clients that render HTML emails would otherwise execute injected scripts.
    """
    r = EmailRenderer()
    malicious = AttendeeProfile(
        eb_attendee_id="x",
        name='Evil <script>alert("xss")</script>',
        email="evil@x.com",
        org='<img src=x onerror=alert(1)>',
        role=None,
        blurb='<b>Evil</b> at <script>fetch("/")</script>',
        form_qa=[{"question": "<h1>Fake</h1>", "answer": "<i>also fake</i>"}],
        is_known_ccm_contact=False,
        crm_contact_id=None,
        created_at="x",
    )
    html = r.render(_ctx(new_attendees=[malicious]))
    assert "<script>" not in html
    assert "alert(\"xss\")" not in html
    assert "fetch(\"/\")" not in html
    assert "&lt;script&gt;" in html
    assert "onerror=alert" not in html


def test_autoescape_is_active_for_digest_template():
    """Pin the autoescape config: a future tweak that flips autoescape rules
    (e.g., select_autoescape(['xml']) silently disables HTML escaping for .j2)
    would otherwise re-open the XSS hole this module guards against.
    """
    r = EmailRenderer()
    assert r._env.autoescape("digest.html.j2") is True


def test_subject_format_daily():
    r = EmailRenderer()
    subj = r.format_subject_daily("AI in the newsroom", new_count=4, total=47)
    assert subj == "AI in the newsroom — 4 new registrations (47 total)"


def test_subject_format_initial():
    r = EmailRenderer()
    subj = r.format_subject_initial("AI in the newsroom", total=47)
    assert subj == "AI in the newsroom — initial attendee briefing (47 total)"
