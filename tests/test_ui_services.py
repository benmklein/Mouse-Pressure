from mouse_pressure.ui.stroke_analysis import stroke_analysis_data
from mouse_pressure.ui.windows_shell import asset_path


def test_packaged_shell_assets_exist() -> None:
    assert asset_path("lucide_mouse.ico").is_file()
    assert asset_path("lucide_mouse.png").is_file()


def test_empty_stroke_analysis_is_safe() -> None:
    result = stroke_analysis_data({})

    assert result["path_px"] == 0.0
    assert result["raw"] == []
    assert result["mapped"] == []
    assert result["injected_distance"] == []
