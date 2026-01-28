"""
Image generation for event featured images.

Uses Gemini AI to generate complete event banner images including text.
"""

import io
from pathlib import Path
from typing import Optional

import requests
from PIL import Image

from google import genai
from google.genai import types

from config import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    CCM_BRAND,
    IMAGE_SETTINGS,
    TEMP_DIR,
)
from airtable_client import EventRecord


# Full image generation prompt
IMAGE_PROMPT_TEMPLATE = """Create a professional event promotional banner image.

EVENT TITLE: "{title}"
SUBTITLE: "{brief_description}"

DESIGN REQUIREMENTS:
- Canvas size: 2048x1024 pixels (2:1 aspect ratio)
- The image should be a COMPLETE, polished promotional banner ready for Eventbrite
- Include ONLY TWO text elements:
  1. The event title "{title}" as bold, prominent text
  2. The subtitle "{brief_description}" as smaller supporting text
- Text should be large, readable, and professionally styled

CRITICAL TEXT FORMATTING:
- Use sentence case: capitalize the first letter of the sentence and proper nouns only
- DO NOT use Title Case (capitalizing Every Word) or ALL CAPS
- DO NOT use all lowercase either
- Correct examples:
  * "2026 NJNC Excellence in local news awards" (sentence case - correct)
  * "How to cover elections in New Jersey" (sentence case - correct)
- Incorrect examples:
  * "2026 NJNC Excellence In Local News Awards" (Title Case - wrong)
  * "2026 njnc excellence in local news awards" (all lowercase - wrong)
  * "2026 NJNC EXCELLENCE IN LOCAL NEWS AWARDS" (ALL CAPS - wrong)
- Keep acronyms capitalized: NJNC, NJ, AI, CCM

STYLE DIRECTION:
- Full-bleed design - the visual should fill the entire canvas
- Can be: illustrated scene, stylized graphic, or artistic composition
- Bold typography integrated naturally into the composition
- Clean, modern, professional aesthetic
- Leave appropriate space for text to be readable

COLOR PALETTE:
- Primary: Black (#000000) and white for text contrast
- Accent options: Teal (#38E6CF), Yellow (#F7E94A), or red (#CA3553)
- Background can be muted/subtle to let text stand out

DO NOT:
- Use Title Case or ALL CAPS for text (use sentence case only)
- Make the image look like generic stock art or clip art
- Use busy patterns that compete with text
- Make text hard to read
- Create a rigid "left text / right visual" split layout
- Include any logos or organizational branding
- Add any text beyond the title and subtitle

INSPIRATION: Think illustrated editorial graphics, modern event posters, or stylized journalistic imagery. The design should feel creative and engaging, not corporate or templated.
"""


class ImageGenerator:
    """Generates complete event banner images using Gemini AI."""

    def __init__(self):
        self.client = genai.Client(api_key=GEMINI_API_KEY)
        self._logo_cache: Optional[Image.Image] = None

    def generate_event_image(
        self,
        event: EventRecord,
        output_path: Optional[Path] = None,
    ) -> Path:
        """
        Generate a complete event banner image.

        Args:
            event: EventRecord with event details
            output_path: Where to save the image. If None, uses temp directory.

        Returns:
            Path to the generated image
        """
        if output_path is None:
            output_path = TEMP_DIR / f"event_{event.record_id}.png"

        print(f"Generating image for: {event.title}")

        try:
            # Generate complete image with Gemini
            image = self._generate_complete_image(event)

            # Resize to exact dimensions if needed
            target_size = (IMAGE_SETTINGS["width"], IMAGE_SETTINGS["height"])
            if image.size != target_size:
                image = image.resize(target_size, Image.Resampling.LANCZOS)

            # Save the image
            image.save(output_path, "PNG", quality=95)
            print(f"Image saved to: {output_path}")
            return output_path

        except Exception as e:
            print(f"Error generating image: {e}")
            print("Using fallback branded image...")
            return self._create_fallback_image(event.title, output_path)

    def _add_logo(self, image: Image.Image) -> Image.Image:
        """Overlay the CCM logo on the image."""
        logo = self._get_logo()
        if logo is None:
            return image

        # Resize logo to appropriate size (max 200px wide)
        max_width = 200
        if logo.width > max_width:
            ratio = max_width / logo.width
            new_size = (int(logo.width * ratio), int(logo.height * ratio))
            logo = logo.resize(new_size, Image.Resampling.LANCZOS)

        # Position in top-left with padding
        padding = 40
        position = (padding, padding)

        # Paste logo with transparency handling
        if logo.mode == "RGBA":
            image.paste(logo, position, logo)
        else:
            image.paste(logo, position)

        return image

    def _get_logo(self) -> Optional[Image.Image]:
        """Download and cache the CCM logo."""
        if self._logo_cache is not None:
            return self._logo_cache

        # Try color logo first, fall back to white if needed
        logo_url = CCM_BRAND["logo_color"]

        try:
            response = requests.get(logo_url, timeout=10)
            response.raise_for_status()
            self._logo_cache = Image.open(io.BytesIO(response.content))
            return self._logo_cache
        except Exception as e:
            print(f"  Warning: Could not download logo: {e}")
            return None

    def _generate_complete_image(self, event: EventRecord) -> Image.Image:
        """Generate the complete event banner using Gemini."""
        prompt = IMAGE_PROMPT_TEMPLATE.format(
            title=event.title,
            brief_description=event.brief_description or "Professional journalism event",
            event_type=event.event_type or "Virtual event",
        )

        print("  Generating with Gemini...")

        response = self.client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
            ),
        )

        # Extract image from response
        for part in response.candidates[0].content.parts:
            if part.inline_data is not None:
                image_data = part.inline_data.data
                image = Image.open(io.BytesIO(image_data))
                return image.convert("RGB")

        raise ValueError("No image returned from Gemini")

    def _create_fallback_image(self, title: str, output_path: Path) -> Path:
        """Create a simple branded fallback image."""
        from PIL import ImageDraw, ImageFont

        width = IMAGE_SETTINGS["width"]
        height = IMAGE_SETTINGS["height"]

        # Create dark background
        canvas = Image.new("RGB", (width, height), "#1a1a1a")
        draw = ImageDraw.Draw(canvas)

        # Add accent bar at bottom
        accent_height = 8
        draw.rectangle(
            [0, height - accent_height, width, height],
            fill=CCM_BRAND["accent_color"],
        )

        # Add title text
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 72)
        except:
            font = ImageFont.load_default()

        # Center the text
        bbox = draw.textbbox((0, 0), title, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        x = (width - text_width) // 2
        y = (height - text_height) // 2

        draw.text((x, y), title, font=font, fill="#FFFFFF")

        # Add CCM text in corner
        try:
            small_font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 24)
        except:
            small_font = ImageFont.load_default()

        draw.text((40, 40), "CENTER FOR COOPERATIVE MEDIA", font=small_font, fill="#888888")

        canvas.save(output_path, "PNG")
        print(f"Fallback image saved to: {output_path}")
        return output_path


def test_image_generation():
    """Test image generation with a sample event."""
    from datetime import datetime

    test_event = EventRecord(
        record_id="test_v2",
        title="How journalism collaboratives can raise money from small-dollar donors",
        brief_description="Learn strategies for sustainable funding through grassroots donor campaigns",
        full_description="Join us for an in-depth session on building donor programs.",
        start_datetime=datetime.now(),
        end_datetime=None,
        event_type="Virtual",
        pricing="Free",
        speaker_info=None,
    )

    generator = ImageGenerator()
    output_path = TEMP_DIR / "test_complete_image.png"
    result = generator.generate_event_image(test_event, output_path)
    print(f"Test image generated: {result}")
    return result


if __name__ == "__main__":
    test_image_generation()
