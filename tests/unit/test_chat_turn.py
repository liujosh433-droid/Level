"""Chat router path allow-list — no production name literals."""

from level_api.services.chat_turn import normalize_chat_path


def test_normalize_chat_path_allow_list() -> None:
    assert normalize_chat_path("schedule") == "schedule"
    assert normalize_chat_path("EMAIL") == "email"
    assert normalize_chat_path("profile") == "profile"
    assert normalize_chat_path("general") == "general"
    assert normalize_chat_path("invent-people") == "general"
    assert normalize_chat_path("") == "general"
