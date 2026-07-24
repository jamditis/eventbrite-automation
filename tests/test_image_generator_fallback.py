"""The image generator must degrade to the default CCM banner — and say so —
when Gemini fails, instead of crashing the pipeline or silently substituting.
"""

from types import SimpleNamespace

import image_generator
from airtable_client import EventRecord


def _event():
    return EventRecord(
        record_id="recTEST0000001",
        title="Fallback test",
        brief_description="Short",
        full_description="Body",
        start_datetime=None,
        end_datetime=None,
        event_type="Virtual",
        pricing="Free",
    )


def _generator(monkeypatch):
    monkeypatch.setattr(
        image_generator.genai, "Client", lambda **kwargs: SimpleNamespace()
    )
    return image_generator.ImageGenerator()


def test_gemini_failure_uses_fallback_and_flags_it(monkeypatch, tmp_path):
    gen = _generator(monkeypatch)

    def boom(event):
        raise ValueError("No image returned from Gemini")

    monkeypatch.setattr(gen, "_generate_complete_image", boom)

    out = tmp_path / "banner.png"
    result = gen.generate_event_image(_event(), output_path=out)

    assert result.used_fallback is True
    assert "No image returned" in result.error
    assert result.path == out
    assert out.exists() and out.stat().st_size > 0


def test_successful_generation_is_not_flagged(monkeypatch, tmp_path):
    from PIL import Image

    gen = _generator(monkeypatch)
    monkeypatch.setattr(
        gen, "_generate_complete_image", lambda event: Image.new("RGB", (2048, 1024))
    )

    out = tmp_path / "banner.png"
    result = gen.generate_event_image(_event(), output_path=out)

    assert result.used_fallback is False
    assert result.error is None
    assert out.exists()
