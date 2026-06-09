"""
Tests for template runtime theme/design switching via refactored theme services.

Covers:
- Fast, Bootstrap, Vanilla, and custom Design templates
- Document theme sync on design changes
- Component parameter updates (Fast style params, Bootstrap html_attrs)
- HoloViews bokeh_theme propagation
- Viewable state preservation across design switches
- URL theme query parameter resolution
- Soft reload trigger logic
- Snapshot state migration
"""
from __future__ import annotations

import typing as t
from unittest.mock import MagicMock, patch

import pytest
from bokeh.document import Document

from panel.config import config
from panel.io.state import state, set_curdoc
from panel.pane import HoloViews
from panel.template import BootstrapTemplate, VanillaTemplate
from panel.template.fast import FastListTemplate
from panel.template.theme_services import (
    ComponentThemeUpdater,
    DesignResolver,
    SnapshotStateMigrator,
    SoftReloadService,
    ThemeSynchronizer,
)
from panel.theme import Bootstrap, DefaultTheme, Design, Material, Native
from panel.theme.base import DarkTheme
from panel.theme.bootstrap import BootstrapDarkTheme, BootstrapDefaultTheme
from panel.theme.fast import Fast, FastDarkTheme, FastDefaultTheme, FastStyle
from panel.viewable import Viewable
from panel.widgets import Button, FloatSlider


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_document():
    doc = Document()
    return doc


@pytest.fixture
def template_with_doc(fresh_document):
    tmpl = VanillaTemplate(title="Test")
    tmpl.server_doc(fresh_document)
    return tmpl, fresh_document


class _CustomTheme(DefaultTheme):
    pass


class _CustomDarkTheme(DarkTheme):
    pass


class _CustomDesign(Design):
    _themes = {
        "default": _CustomTheme,
        "dark": _CustomDarkTheme,
    }


# ---------------------------------------------------------------------------
# DesignResolver tests
# ---------------------------------------------------------------------------


class TestDesignResolver:

    def test_resolve_design_class_explicit_param(self):
        params = {"design": Material}
        assert DesignResolver.resolve_design_class(VanillaTemplate, params) is Material

    def test_resolve_design_class_inherits_config(self):
        with config.set(design=Material):
            assert DesignResolver.resolve_design_class(VanillaTemplate, {}) is Material

    def test_resolve_design_class_explicit_overrides_config(self):
        with config.set(design=Material):
            params = {"design": Bootstrap}
            assert DesignResolver.resolve_design_class(VanillaTemplate, params) is Bootstrap

    def test_resolve_design_class_fast_default(self):
        assert DesignResolver.resolve_design_class(FastListTemplate, {}) is Fast

    def test_resolve_theme_class_from_string(self):
        assert DesignResolver.resolve_theme_class("dark") is DarkTheme
        assert DesignResolver.resolve_theme_class("default") is DefaultTheme

    def test_resolve_theme_class_from_class(self):
        assert DesignResolver.resolve_theme_class(FastDarkTheme) is FastDarkTheme

    def test_resolve_theme_class_fallback_config(self):
        with config.set(theme="dark"):
            assert DesignResolver.resolve_theme_class(None) is DarkTheme

    def test_resolve_query_theme(self):
        with patch.object(
            type(state),
            'session_args',
            new_callable=lambda: property(lambda self: {"theme": [b"dark"]}),
        ):
            assert DesignResolver.resolve_query_theme() == "dark"

        with patch.object(
            type(state),
            'session_args',
            new_callable=lambda: property(lambda self: {"theme": [b'"default"']}),
        ):
            assert DesignResolver.resolve_query_theme() == "default"

        with patch.object(
            type(state),
            'session_args',
            new_callable=lambda: property(lambda self: {}),
        ):
            assert DesignResolver.resolve_query_theme() is None

    def test_instantiate(self):
        design = DesignResolver.instantiate(Fast, FastDarkTheme)
        assert isinstance(design, Fast)
        assert isinstance(design.theme, FastDarkTheme)


# ---------------------------------------------------------------------------
# ThemeSynchronizer tests
# ---------------------------------------------------------------------------


class TestThemeSynchronizer:

    def test_sync_to_document(self, fresh_document):
        design = Fast(theme=FastDarkTheme)
        ThemeSynchronizer.sync_to_document(design, fresh_document)
        assert fresh_document.theme is not None
        bg = fresh_document.theme._json["attrs"]["figure"]["background_fill_color"]
        assert bg == "#181818"

    def test_sync_to_config(self, fresh_document):
        design = Bootstrap(theme=BootstrapDarkTheme)
        with set_curdoc(fresh_document):
            ThemeSynchronizer.sync_to_config(design, fresh_document)
            assert config.design is Bootstrap

    def test_ensure_hook_registered(self):
        tmpl = VanillaTemplate()
        col = tmpl._layout
        hook_count_before = len(col._hooks)
        ThemeSynchronizer.ensure_hook_registered(tmpl._design, col)
        assert tmpl._design._apply_hooks in col._hooks
        ThemeSynchronizer.ensure_hook_registered(tmpl._design, col)
        assert len(col._hooks) == hook_count_before + 1


# ---------------------------------------------------------------------------
# ComponentThemeUpdater tests
# ---------------------------------------------------------------------------


class TestComponentThemeUpdater:

    def test_sync_template_params_from_style_fast(self):
        tmpl = FastListTemplate()
        style = FastStyle(
            accent_base_color="#FF0000",
            background_color="#00FF00",
            corner_radius=10,
        )

        class _FakeTheme:
            pass

        fake_theme = _FakeTheme()
        fake_theme.style = style

        class _FakeDesign:
            pass

        fake_design = _FakeDesign()
        fake_design.theme = fake_theme

        tmpl.accent_base_color = "#0072B5"
        ComponentThemeUpdater.sync_template_params_from_style(tmpl, fake_design)
        assert tmpl.accent_base_color == "#FF0000"
        assert tmpl.background_color == "#00FF00"
        assert tmpl.corner_radius == 10

    def test_sync_template_params_respects_override(self):
        tmpl = FastListTemplate()
        style = FastStyle(accent_base_color="#FF0000")

        class _FakeTheme:
            pass

        fake_theme = _FakeTheme()
        fake_theme.style = style

        class _FakeDesign:
            pass

        fake_design = _FakeDesign()
        fake_design.theme = fake_theme

        ComponentThemeUpdater.sync_template_params_from_style(
            tmpl, fake_design, override_params={"accent_base_color"}
        )
        assert tmpl.accent_base_color == "#0072B5"

    def test_sync_style_from_template_params_fast(self):
        tmpl = FastListTemplate(accent_base_color="#123456")
        style = FastStyle()

        class _FakeTheme:
            pass

        fake_theme = _FakeTheme()
        fake_theme.style = style

        class _FakeDesign:
            pass

        fake_design = _FakeDesign()
        fake_design.theme = fake_theme

        ComponentThemeUpdater.sync_style_from_template_params(tmpl, fake_design)
        assert style.accent_base_color == "#123456"

    def test_get_bootstrap_html_attrs(self):
        light = Bootstrap(theme=BootstrapDefaultTheme)
        dark = Bootstrap(theme=BootstrapDarkTheme)
        assert ComponentThemeUpdater.get_bootstrap_html_attrs(light) == 'data-bs-theme="light"'
        assert ComponentThemeUpdater.get_bootstrap_html_attrs(dark) == 'data-bs-theme="dark"'

    def test_update_holoviews_themes(self):
        try:
            import holoviews as hv
        except ImportError:
            pytest.skip("holoviews not installed")

        curve = hv.Curve([1, 2, 3])
        pane = HoloViews(curve)
        design = Fast(theme=FastDarkTheme)
        ComponentThemeUpdater.update_holoviews_themes([pane], design)
        assert pane.theme is not None

    def test_collect_render_variables(self):
        tmpl = FastListTemplate()
        variables: dict[str, t.Any] = {}
        ComponentThemeUpdater.collect_render_variables(tmpl, tmpl._design, variables)
        assert variables["theme"] is tmpl._design.theme


# ---------------------------------------------------------------------------
# SoftReloadService tests
# ---------------------------------------------------------------------------


class TestSoftReloadService:

    def test_should_reload_design_type_change(self):
        old = Fast(theme=FastDefaultTheme)
        new = Bootstrap(theme=BootstrapDefaultTheme)
        assert SoftReloadService.should_reload(old, new) is True

    def test_should_reload_theme_change(self):
        old = Fast(theme=FastDefaultTheme)
        new = Fast(theme=FastDarkTheme)
        assert SoftReloadService.should_reload(old, new) is True

    def test_should_reload_none_old(self):
        new = Fast(theme=FastDefaultTheme)
        assert SoftReloadService.should_reload(None, new) is False

    def test_should_reload_same(self):
        old = Fast(theme=FastDefaultTheme)
        same = Fast(theme=FastDefaultTheme)
        assert SoftReloadService.should_reload(old, same) is False

    def test_get_theme_name(self):
        light = Fast(theme=FastDefaultTheme)
        dark = Fast(theme=FastDarkTheme)
        assert SoftReloadService.get_theme_name(light) == "default"
        assert SoftReloadService.get_theme_name(dark) == "dark"

    def test_trigger_reload_updates_location_query(self, fresh_document):
        from panel.io.location import Location

        tmpl = FastListTemplate(title="t")
        tmpl._documents.append(fresh_document)
        loc = MagicMock(spec=Location)
        loc.search = ""
        state._locations[fresh_document] = loc

        try:
            SoftReloadService.trigger_reload(tmpl, "dark")
            loc.update_query.assert_called_once_with(theme="dark")
        finally:
            if fresh_document in state._locations:
                del state._locations[fresh_document]


# ---------------------------------------------------------------------------
# SnapshotStateMigrator tests
# ---------------------------------------------------------------------------


class TestSnapshotStateMigrator:

    def test_snapshot_restore_viewable_state(self):
        w = FloatSlider(value=5)
        migrator = SnapshotStateMigrator()
        migrator.snapshot_viewable_state(w, "ref1")
        original_models = dict(w._models)
        w._models["newref"] = "temporary"
        migrator.restore_viewable_state(w, "ref1")
        for k in original_models:
            assert k in w._models

    def test_snapshot_restore_document(self, fresh_document):
        migrator = SnapshotStateMigrator()
        fresh_document._template_variables["foo"] = "bar"
        snap = migrator.snapshot_document(fresh_document)
        del fresh_document._template_variables["foo"]
        migrator.restore_document(fresh_document, snap)
        assert fresh_document._template_variables.get("foo") == "bar"

    def test_migrate_design_swaps_hooks(self):
        tmpl = VanillaTemplate()
        btn = Button(label="x")
        tmpl._render_items["btn"] = (btn, ["main"])
        old_design = Native()
        new_design = Material()
        btn._hooks.append(old_design._apply_hooks)

        migrator = SnapshotStateMigrator()
        migrator.migrate_design(old_design, new_design, tmpl)

        assert old_design._apply_hooks not in btn._hooks
        assert new_design._apply_hooks in btn._hooks


# ---------------------------------------------------------------------------
# Template integration tests — runtime design switching
# ---------------------------------------------------------------------------


class TestTemplateRuntimeDesignSwitching:

    def test_vanilla_template_runtime_design_switch_updates_doc_theme(self, fresh_document):
        tmpl = VanillaTemplate()
        tmpl.server_doc(fresh_document)
        initial_theme = fresh_document.theme

        tmpl.design = Material
        assert isinstance(tmpl._design, Material)
        assert isinstance(tmpl._design.theme, DefaultTheme)
        assert fresh_document.theme is not initial_theme

    def test_bootstrap_template_updates_html_attrs_in_vars(self):
        tmpl = BootstrapTemplate()
        tmpl._update_vars()
        assert tmpl._render_variables["html_attrs"] == 'data-bs-theme="light"'

    def test_fast_template_style_params_sync_on_init(self):
        tmpl = FastListTemplate(accent_base_color="#AABBCC")
        assert tmpl._design.theme.style.accent_base_color == "#AABBCC"

    def test_fast_template_style_updates_write_back_to_style(self):
        tmpl = FastListTemplate()
        tmpl.accent_base_color = "#112233"
        tmpl._update_vars()
        assert tmpl._design.theme.style.accent_base_color == "#112233"

    def test_custom_design_works_with_vanilla_template(self):
        with config.set(design=_CustomDesign):
            tmpl = VanillaTemplate()
        assert tmpl.design is _CustomDesign
        assert isinstance(tmpl._design, _CustomDesign)
        assert isinstance(tmpl._design.theme, _CustomTheme)

    def test_runtime_design_switch_preserves_render_items(self):
        tmpl = VanillaTemplate()
        btn = Button(label="Keep")
        tmpl.main.append(btn)
        ref = f"main-{id(btn)}"
        assert ref in tmpl._render_items

        tmpl.design = Bootstrap
        assert isinstance(tmpl._design, Bootstrap)
        assert ref in tmpl._render_items
        assert tmpl._render_items[ref][0] is btn

    def test_viewable_hooks_migrate_on_design_change(self):
        tmpl = VanillaTemplate()
        btn = Button(label="test")
        tmpl.main.append(btn)
        old_hook = tmpl._design._apply_hooks
        btn._hooks.append(old_hook)
        assert old_hook in btn._hooks

        tmpl.design = Material
        new_hook = tmpl._design._apply_hooks
        assert old_hook not in btn._hooks
        assert new_hook in btn._hooks

    def test_url_theme_parameter_applied_to_fast_template(self):
        with patch.object(
            type(state),
            'session_args',
            new_callable=lambda: property(lambda self: {"theme": [b"dark"]}),
        ):
            tmpl = FastListTemplate(title="t")
            assert isinstance(tmpl._design.theme, FastDarkTheme)

    def test_all_services_available_on_every_template(self):
        for cls in (VanillaTemplate, BootstrapTemplate, FastListTemplate):
            tmpl = cls()
            assert isinstance(tmpl.design_resolver, DesignResolver)
            assert isinstance(tmpl.theme_synchronizer, ThemeSynchronizer)
            assert isinstance(tmpl.component_theme_updater, ComponentThemeUpdater)
            assert isinstance(tmpl.soft_reload_service, SoftReloadService)
            assert isinstance(tmpl.state_migrator, SnapshotStateMigrator)

    def test_setup_design_syncs_to_all_documents(self, fresh_document):
        tmpl = VanillaTemplate()
        tmpl.server_doc(fresh_document)
        with set_curdoc(fresh_document):
            assert config.design is Native

        tmpl.design = Bootstrap
        with set_curdoc(fresh_document):
            assert config.design is Bootstrap
