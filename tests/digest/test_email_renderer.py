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
        admin_url="x",
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


def test_render_shows_new_attendee_blurbs_and_qa():
    r = EmailRenderer()
    html = r.render(
        _ctx(
            new_attendees=[
                _profile(qa=[{"question": "What do you hope to learn?", "answer": "AI"}])
            ]
        )
    )
    assert "Sarah Smith — NJJ, Editor." in html
    assert "What do you hope to learn?" in html
    assert "<dd" in html  # Q&A block actually rendered with definition list


def test_render_existing_section_only_shows_name_and_org_no_qa():
    r = EmailRenderer()
    profile_with_qa = _profile(
        qa=[{"question": "SHOULD_NOT_RENDER_IN_EXISTING", "answer": "X"}]
    )
    html = r.render(_ctx(total_count=2, existing_attendees=[profile_with_qa]))
    assert "Sarah Smith — NJJ" in html
    assert "SHOULD_NOT_RENDER_IN_EXISTING" not in html


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
