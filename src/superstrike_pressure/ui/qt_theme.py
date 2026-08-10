"""Theme tokens for the PySide6 desktop UI."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    name: str
    window: str
    sidebar: str
    surface: str
    surface_alt: str
    border: str
    text: str
    muted: str
    accent: str
    accent_hover: str
    accent_soft: str
    success: str
    warning: str
    danger: str
    terminal: str
    terminal_text: str
    grid: str


LIGHT = Theme(
    name="light",
    window="#F5F6F8",
    sidebar="#FFFFFF",
    surface="#FFFFFF",
    surface_alt="#F1F3F6",
    border="#DDE1E7",
    text="#171A20",
    muted="#535C69",
    accent="#635BFF",
    accent_hover="#5148EB",
    accent_soft="#ECEBFF",
    success="#138A5B",
    warning="#B56A00",
    danger="#C33C4A",
    terminal="#15181E",
    terminal_text="#D9DEE8",
    grid="#E5E8ED",
)

DARK = Theme(
    name="dark",
    window="#111318",
    sidebar="#171A20",
    surface="#1B1F26",
    surface_alt="#242932",
    border="#303641",
    text="#F2F4F7",
    muted="#BBC3CF",
    accent="#8B83FF",
    accent_hover="#9D96FF",
    accent_soft="#29264E",
    success="#4CC38A",
    warning="#F1B35B",
    danger="#FF7182",
    terminal="#0C0E12",
    terminal_text="#D9DEE8",
    grid="#2A3039",
)


def theme_for(name: str) -> Theme:
    return DARK if name.lower() == "dark" else LIGHT


def stylesheet(theme: Theme) -> str:
    """Return one token-driven stylesheet for all standard Qt widgets."""
    return f"""
    * {{
        font-family: "Segoe UI";
        font-size: 13px;
        color: {theme.text};
    }}
    QMainWindow, QWidget#appRoot, QWidget#page {{ background: {theme.window}; }}
    QWidget#sidebar {{
        background: {theme.sidebar};
        border-right: 1px solid {theme.border};
    }}
    QLabel#brand {{ font-size: 17px; font-weight: 700; }}
    QLabel#pageTitle {{ font-size: 24px; font-weight: 700; }}
    QLabel#sectionTitle {{ font-size: 15px; font-weight: 650; }}
    QLabel#muted, QLabel.muted {{ color: {theme.muted}; }}
    QLabel#statusRunning {{ color: {theme.success}; font-weight: 600; }}
    QLabel#statusStopped {{ color: {theme.muted}; font-weight: 600; }}
    QLabel#statusBusy {{ color: {theme.warning}; font-weight: 600; }}
    QLabel#statusError {{ color: {theme.danger}; font-weight: 600; }}
    QFrame#card {{
        background: {theme.surface};
        border: 1px solid {theme.border};
        border-radius: 12px;
    }}
    QFrame#statCard {{
        background: {theme.surface_alt};
        border: 1px solid {theme.border};
        border-radius: 9px;
    }}
    QPushButton {{
        background: {theme.surface};
        border: 1px solid {theme.border};
        border-radius: 8px;
        padding: 7px 12px;
        min-height: 20px;
    }}
    QPushButton:hover {{ background: {theme.surface_alt}; }}
    QPushButton:pressed {{ background: {theme.accent_soft}; }}
    QPushButton:disabled {{ color: {theme.muted}; background: {theme.surface_alt}; }}
    QPushButton#primary {{
        color: white;
        background: {theme.accent};
        border-color: {theme.accent};
        font-weight: 650;
        padding: 8px 18px;
    }}
    QPushButton#primary:hover {{ background: {theme.accent_hover}; }}
    QPushButton#danger {{ color: {theme.danger}; }}
    QPushButton#nav {{
        background: transparent;
        border: none;
        border-radius: 8px;
        text-align: left;
        padding: 9px 12px;
        color: {theme.muted};
    }}
    QPushButton#nav:hover {{ color: {theme.text}; background: {theme.surface_alt}; }}
    QPushButton#nav:checked {{
        color: {theme.accent};
        background: {theme.accent_soft};
        font-weight: 650;
    }}
    QToolButton {{
        background: transparent;
        border: none;
        color: {theme.muted};
        padding: 5px;
    }}
    QToolButton:hover {{ color: {theme.text}; background: {theme.surface_alt}; border-radius: 6px; }}
    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
        background: {theme.surface_alt};
        border: 1px solid {theme.border};
        border-radius: 7px;
        padding: 6px 8px;
        min-height: 20px;
        selection-background-color: {theme.accent};
    }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
        border: 1px solid {theme.accent};
    }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox QAbstractItemView {{
        background: {theme.surface}; border: 1px solid {theme.border};
        selection-background-color: {theme.accent_soft};
    }}
    QTabWidget::pane {{ border: none; background: transparent; }}
    QTabBar::tab {{
        background: transparent;
        color: {theme.muted};
        border: none;
        padding: 8px 14px;
        margin-right: 4px;
    }}
    QTabBar::tab:selected {{
        color: {theme.accent};
        background: {theme.accent_soft};
        border-radius: 7px;
        font-weight: 650;
    }}
    QScrollArea {{ background: {theme.window}; border: none; }}
    QScrollArea QWidget#qt_scrollarea_viewport {{ background: {theme.window}; }}
    QScrollArea#settingsScroll,
    QScrollArea#settingsScroll QWidget#qt_scrollarea_viewport,
    QWidget#settingsEditor {{ background: {theme.surface}; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {theme.border}; border-radius: 4px; min-height: 28px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QSlider::groove:horizontal {{ height: 4px; background: {theme.border}; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {theme.accent}; border-radius: 2px; }}
    QSlider::handle:horizontal {{
        background: {theme.surface};
        border: 2px solid {theme.accent};
        width: 14px; height: 14px; margin: -6px 0; border-radius: 8px;
    }}
    QCheckBox {{ spacing: 7px; }}
    QCheckBox::indicator {{
        width: 15px; height: 15px;
        border: 1px solid {theme.border};
        border-radius: 4px;
        background: {theme.surface_alt};
    }}
    QCheckBox::indicator:checked {{
        background: {theme.accent};
        border-color: {theme.accent};
    }}
    QPlainTextEdit {{
        background: {theme.terminal}; color: {theme.terminal_text};
        border: 1px solid {theme.border}; border-radius: 9px;
        padding: 10px; font-family: "Cascadia Mono", Consolas, monospace;
        font-size: 12px;
    }}
    QMenu {{ background: {theme.surface}; border: 1px solid {theme.border}; padding: 5px; }}
    QMenu::item {{ padding: 7px 22px 7px 10px; border-radius: 5px; }}
    QMenu::item:selected {{ background: {theme.accent_soft}; color: {theme.accent}; }}
    QToolTip {{ background: {theme.surface}; color: {theme.text}; border: 1px solid {theme.border}; padding: 5px; }}
    """
