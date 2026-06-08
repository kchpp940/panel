"""
ThemeManager model for runtime theme switching.
"""
from bokeh.core.properties import (
    Any, Bool, Dict, String,
)
from bokeh.models import Model


class ThemeManager(Model):
    """
    A bokeh model that manages theme state and notifies the frontend
    about theme/design changes at runtime.
    """

    design = String(default='native', help="""
        The name of the current design system (e.g. 'fast', 'bootstrap', 'material', 'native').""")

    theme = String(default='default', help="""
        The name of the current color theme ('default' or 'dark').""")

    theme_name = String(default='', help="""
        The full theme class name (e.g. 'FastDarkTheme').""")

    base_css = String(default='', help="""
        The base CSS content with CSS variables for the current theme.""")

    theme_css = String(default='', help="""
        Additional theme-specific CSS content.""")

    css_variables = Dict(String, Any, default={}, help="""
        Dictionary of CSS variable names to values for the current theme.""")

    design_name = String(default='', help="""
        Human-readable design name.""")

    is_dark = Bool(default=False, help="""
        Whether the current theme is a dark theme.""")

    bokeh_theme_json = Dict(String, Any, default={}, help="""
        JSON representation of the Bokeh theme to apply to plots.""")

    resources = Dict(String, Dict(String, String), default={}, help="""
        Design resources grouped by type: 'css', 'js', 'js_modules', 'font'.
        Each maps a resource name to its URL/path.""")

    extension_themes = Dict(String, Any, default={}, help="""
        Theme configuration for extension components. Keys include:
        'tabulator_theme' (str), 'tabulator_theme_classes' (list),
        'echarts_theme_light' (str), 'echarts_theme_dark' (str),
        'plotly_template_light' (str), 'plotly_template_dark' (str).""")

    fast_style = Dict(String, Any, default={}, help="""
        Fast Design-specific style parameters: accent_base_color, neutral_color,
        background_color, luminance, corner_radius, etc. Only populated for Fast design.""")

    bs_theme = String(default='light', help="""
        Bootstrap theme attribute: 'light' or 'dark'. Only populated for Bootstrap design.""")
