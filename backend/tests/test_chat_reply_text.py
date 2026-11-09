"""Newer Claude models reply with content blocks; only text reaches the user."""

from app.chat.service import SYSTEM_PROMPT, _reply_text


def test_thinking_blocks_and_signatures_are_never_shown():
    content = [
        {"type": "thinking", "thinking": "", "signature": "CAQS+secret-signature=="},
        {"type": "text", "text": "This is PaySilo, a payroll platform."},
    ]
    text = _reply_text(content)
    assert text == "This is PaySilo, a payroll platform."
    assert "signature" not in text and "thinking" not in text


def test_multiple_text_blocks_are_joined_and_plain_strings_pass_through():
    assert _reply_text([{"type": "text", "text": "One."}, {"type": "text", "text": "Two."}]) == "One.\n\nTwo."
    assert _reply_text("Plain reply") == "Plain reply"
    assert _reply_text([{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]) == ""


def test_system_prompt_lets_silo_explain_what_paysilo_is():
    for phrase in ("Starter Kit 06", "connected account", "smallest lines", "Visa TAP", "Metal"):
        assert phrase in SYSTEM_PROMPT
