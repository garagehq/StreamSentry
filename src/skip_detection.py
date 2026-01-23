"""
Skip button detection for Minus.

Detects "Skip Ad" buttons in OCR results for automatic ad skipping.
"""

import re


def check_skip_opportunity(all_texts: list) -> tuple:
    """
    Check OCR results for skippable "Skip" button.

    For YouTube/Fire TV ads:
    - "Skip" alone = skippable NOW
    - "Skip Ad" = skippable NOW
    - "Skip in" (no number) = skippable NOW (OCR may miss the number when it hits 0)
    - "Skip 5" or "Skip Ad in 5" = NOT skippable (countdown active)
    - "skip" in lowercase = skippable NOW (Fire TV shows lowercase when ready)

    Args:
        all_texts: List of detected text strings from OCR

    Returns:
        Tuple of (is_skippable, skip_text, countdown_seconds)
        - is_skippable: True if skip button is ready to press
        - skip_text: The detected skip-related text
        - countdown_seconds: Countdown remaining (0 if skippable, >0 if countdown)
    """
    for text in all_texts:
        text_lower = text.lower().strip()

        # Check for "Skip" with countdown number FIRST
        # Patterns: "Skip 5", "Skip Ad in 5", "Skip in 5s", "Skip 10", etc.
        # Must have an actual digit to be considered a countdown
        countdown_match = re.search(r'skip\s*(?:ad\s*)?(?:in\s*)?(\d+)\s*s?', text_lower)
        if countdown_match:
            countdown = int(countdown_match.group(1))
            if countdown > 0:  # Only treat as countdown if > 0
                return (False, text, countdown)
            # countdown == 0 means skippable
            return (True, text, 0)

        # "Skip in" without a number = likely skippable (OCR missed the 0 or it disappeared)
        # This is a common OCR pattern on Fire TV when the skip becomes available
        if re.search(r'^skip\s*in\s*$', text_lower):
            return (True, text, 0)

        # Check for standalone "Skip" or "Skip Ad" (no number = skippable)
        # Also match "skip" in lowercase (Fire TV shows this when skippable)
        if re.search(r'^skip\s*(?:ad|ads)?$', text_lower) and len(text_lower) <= 10:
            return (True, text, 0)

        # Direct matches for common skip button text
        # Also handle "Skip>" and "Skip >" with arrow indicators
        if text_lower in ['skip', 'skip ad', 'skip ads', 'skipad', 'skip in', 'skip>', 'skip >']:
            return (True, text, 0)

        # Handle "Skip" followed by any arrow character or symbol (>, →, ►, etc.)
        if re.match(r'^skip\s*[>\-\u2192\u25ba→►]?\s*$', text_lower):
            return (True, text, 0)

    return (False, None, None)
