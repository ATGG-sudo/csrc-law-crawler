from parser import repair_known_neris_mojibake


def test_repair_known_neris_mojibake_removes_list_marker_artifact() -> None:
    assert repair_known_neris_mojibake("�0�2（二）") == "（二）"


def test_repair_known_neris_mojibake_restores_middle_dot_unit() -> None:
    assert repair_known_neris_mojibake("2元/吨�6�1天") == "2元/吨·天"
