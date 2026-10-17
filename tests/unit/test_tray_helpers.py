from netanchor.ui.tray import color_for_state, status_text


def test_color_for_known_states():
    assert color_for_state("online") == "#2ecc71"
    assert color_for_state("reconnecting") == "#e74c3c"
    assert color_for_state("stopped") == "#95a5a6"


def test_color_for_unknown_state_falls_back_to_grey():
    assert color_for_state("whatever") == "#95a5a6"


def test_status_text_online():
    assert status_text({"state": "online"}) == "状态：已连接"


def test_status_text_includes_error_when_offline():
    text = status_text({"state": "reconnecting", "last_error": "验证码校验未通过"})
    assert "重连中" in text
    assert "验证码校验未通过" in text


def test_status_text_without_error():
    assert status_text({"state": "stopped"}) == "状态：已停止"


def test_status_text_unknown_state_uses_raw_value():
    assert "weird" in status_text({"state": "weird"})
