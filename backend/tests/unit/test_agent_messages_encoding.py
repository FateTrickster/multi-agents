from app.agents import agent_messages as m


def test_message_constants_no_common_garbled_glyphs():
    values = [
        m.SILENCE_STRATEGY,
        m.MONOPOLY_STRATEGY,
        m.MENTION_ACCEPTED,
        m.MENTION_QUEUED,
        m.OBSERVE_REASON,
        m.LOW_SOCIAL_REASON,
        m.LOW_COGNITIVE_REASON,
        m.LOW_EMOTIONAL_REASON,
        m.SUPPRESS_MONOPOLY_REASON,
        m.SUPPRESS_SILENCE_REASON,
    ]
    garbled_markers = ("鏆", "宸", "褰", "涓", "闇")
    joined = "\n".join(values)
    for marker in garbled_markers:
        assert marker not in joined


def test_backend_sources_have_no_encoding_pollution():
    from pathlib import Path

    app_dir = Path(__file__).parents[2] / "app"
    for source_path in app_dir.rglob("*"):
        if source_path.suffix not in {".py", ".txt", ".yaml", ".yml"}:
            continue
        content = source_path.read_text(encoding="utf-8-sig")
        assert "????" not in content, source_path
        assert "�" not in content, source_path
