from unittest.mock import MagicMock

import pytest

from digest.crm_lookup import CrmContact
from digest.eventbrite_client import EventbriteAttendee
from digest.profile_builder import AttendeeProfile, ProfileBuilder


def _attendee(**overrides):
    base = dict(
        id="100001",
        created="2026-05-01T10:00:00Z",
        status="Attending",
        cancelled=False,
        refunded=False,
        first_name="Sarah",
        last_name="Smith",
        email="sarah@example.com",
        name="Sarah Smith",
        answers=[
            {
                "question_id": "q_1",
                "question": "What do you hope to learn?",
                "answer": "AI workflows",
                "type": "text",
            }
        ],
    )
    base.update(overrides)
    return EventbriteAttendee(**base)


@pytest.fixture
def mock_crm():
    m = MagicMock()
    m.find_by_email.return_value = None
    return m


@pytest.fixture
def mock_llm():
    m = MagicMock()
    m.run_blurb.return_value = None
    return m


def test_skips_cancelled_attendee(mock_crm, mock_llm):
    builder = ProfileBuilder(crm=mock_crm, llm=mock_llm)
    cancelled = _attendee(cancelled=True, status="Not Attending")
    assert builder.build(cancelled) is None


def test_skips_not_attending_status(mock_crm, mock_llm):
    builder = ProfileBuilder(crm=mock_crm, llm=mock_llm)
    not_attending = _attendee(status="Not Attending")
    assert builder.build(not_attending) is None


def test_includes_checked_in_status(mock_crm, mock_llm):
    builder = ProfileBuilder(crm=mock_crm, llm=mock_llm)
    checked_in = _attendee(status="Checked In")
    profile = builder.build(checked_in)
    assert profile is not None


def test_empty_email_attendee_skips_crm_and_llm(mock_crm, mock_llm):
    """Privacy invariant adjacency: empty-email attendees can't be CRM-matched,
    so the LLM must not be invoked for them. Pinning this branch prevents a
    future regression that would silently send LLM-generated prose about
    strangers we can't even uniquely identify.
    """
    builder = ProfileBuilder(crm=mock_crm, llm=mock_llm)
    a = _attendee(email="")
    profile = builder.build(a)
    assert profile is not None
    assert profile.is_known_ccm_contact is False
    mock_crm.find_by_email.assert_not_called()
    mock_llm.run_blurb.assert_not_called()


def test_unknown_attendee_uses_form_only_blurb_no_llm(mock_crm, mock_llm):
    """Privacy invariant: LLM is NEVER called for non-CRM contacts."""
    mock_crm.find_by_email.return_value = None
    builder = ProfileBuilder(crm=mock_crm, llm=mock_llm)
    profile = builder.build(_attendee())
    assert isinstance(profile, AttendeeProfile)
    assert profile.is_known_ccm_contact is False
    assert profile.crm_contact_id is None
    assert "Sarah Smith" in profile.blurb
    mock_llm.run_blurb.assert_not_called()


def test_known_attendee_uses_llm_with_crm_data_in_prompt(mock_crm, mock_llm):
    mock_crm.find_by_email.return_value = CrmContact(
        id=42,
        name="Sarah Smith",
        email="sarah@example.com",
        alt_email=None,
        work_email=None,
        organization="North Jersey Journal",
        role="Editor",
        notes="Covers municipal government.",
        interactions=({"summary": "Coffee chat re: AI tools"},),
    )
    mock_llm.run_blurb.return_value = (
        "Sarah Smith edits the North Jersey Journal where she covers "
        "local elections and budget hearings."
    )
    builder = ProfileBuilder(crm=mock_crm, llm=mock_llm)
    profile = builder.build(_attendee())
    assert profile.is_known_ccm_contact is True
    assert profile.crm_contact_id == 42
    assert "edits the North Jersey Journal" in profile.blurb

    mock_llm.run_blurb.assert_called_once()
    sent_prompt = mock_llm.run_blurb.call_args[0][0]
    assert "North Jersey Journal" in sent_prompt
    assert "Editor" in sent_prompt
    assert "Covers municipal government" in sent_prompt
    assert "Coffee chat" in sent_prompt
    assert "AI workflows" in sent_prompt


def test_known_attendee_falls_back_to_template_when_llm_returns_none(mock_crm, mock_llm):
    mock_crm.find_by_email.return_value = CrmContact(
        id=42,
        name="Sarah Smith",
        email="sarah@example.com",
        alt_email=None,
        work_email=None,
        organization="NJJ",
        role="Editor",
        notes="",
        interactions=(),
    )
    mock_llm.run_blurb.return_value = None
    builder = ProfileBuilder(crm=mock_crm, llm=mock_llm)
    profile = builder.build(_attendee())
    assert profile.is_known_ccm_contact is True
    assert "Sarah Smith" in profile.blurb
    assert "NJJ" in profile.blurb


def test_form_qa_preserved_in_output(mock_crm, mock_llm):
    builder = ProfileBuilder(crm=mock_crm, llm=mock_llm)
    profile = builder.build(_attendee())
    assert len(profile.form_qa) == 1
    assert profile.form_qa[0]["question"] == "What do you hope to learn?"
    assert profile.form_qa[0]["answer"] == "AI workflows"


def test_question_filter_applies(mock_crm, mock_llm):
    builder = ProfileBuilder(crm=mock_crm, llm=mock_llm, question_id_filter=["q_2"])
    a = _attendee(
        answers=[
            {"question_id": "q_1", "question": "Q1", "answer": "A1", "type": "text"},
            {"question_id": "q_2", "question": "Q2", "answer": "A2", "type": "text"},
        ]
    )
    profile = builder.build(a)
    assert len(profile.form_qa) == 1
    assert profile.form_qa[0]["question_id"] == "q_2"


def test_question_filter_no_matches_yields_empty_qa(mock_crm, mock_llm):
    builder = ProfileBuilder(crm=mock_crm, llm=mock_llm, question_id_filter=["q_99"])
    profile = builder.build(_attendee())
    assert profile.form_qa == []


def test_deterministic_blurb_extracts_org_from_form_for_unknown(mock_crm, mock_llm):
    """Unknown contact + form has 'organization' Q -> blurb shows that org."""
    mock_crm.find_by_email.return_value = None
    a = _attendee(
        answers=[
            {
                "question_id": "q_org",
                "question": "What organization do you work for?",
                "answer": "ProPublica",
                "type": "text",
            },
            {
                "question_id": "q_role",
                "question": "What is your title or role?",
                "answer": "Senior Reporter",
                "type": "text",
            },
        ]
    )
    builder = ProfileBuilder(crm=mock_crm, llm=mock_llm)
    profile = builder.build(a)
    assert "ProPublica" in profile.blurb
    assert "Senior Reporter" in profile.blurb
