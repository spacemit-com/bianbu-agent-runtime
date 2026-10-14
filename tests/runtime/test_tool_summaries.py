"""Every built-in tool ships a UI intro in both supported languages."""
from bianbu_agent.tools.builtins import BUILTIN_MODULES
from bianbu_agent.tools.summaries import SUMMARY_LANGUAGES, TOOL_SUMMARIES, tool_summaries


def test_every_builtin_tool_has_a_summary_in_every_language():
    missing = sorted(set(BUILTIN_MODULES) - set(TOOL_SUMMARIES))
    assert missing == [], f"built-in tools without a UI summary: {missing}"


def test_no_summary_is_written_for_a_tool_runtime_does_not_own():
    unknown = sorted(set(TOOL_SUMMARIES) - set(BUILTIN_MODULES))
    assert unknown == [], f"summaries for non-builtin tools: {unknown}"


def test_summaries_are_single_line_non_empty_and_complete():
    assert SUMMARY_LANGUAGES == ("zh", "en")
    for name, summary in TOOL_SUMMARIES.items():
        assert sorted(summary) == sorted(SUMMARY_LANGUAGES), name
        for language, text in summary.items():
            assert text == text.strip() and text, (name, language)
            assert "\n" not in text, (name, language)


def test_unknown_tools_have_no_summary():
    assert dict(tool_summaries("vendor_bundle/plugin_echo")) == {}
