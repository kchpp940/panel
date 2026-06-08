from __future__ import annotations

import functools
import os
import pathlib
import re
import typing as t

import param

from bokeh.models import ImportedStyleSheet
from bokeh.themes import Theme as _BkTheme, _dark_minimal, built_in_themes

from ..config import config
from ..custom import PyComponent
from ..io.resources import (
    JS_VERSION, ResourceComponent, component_resource_path, get_dist_path,
    resolve_custom_path,
)
from ..io.state import set_curdoc, state
from ..util import relative_to

if t.TYPE_CHECKING:
    from bokeh.document import Document
    from bokeh.model import Model

    from ..io.resources import ResourceTypes
    from ..viewable import Viewable


class Inherit:
    """
    Singleton object to declare stylesheet inheritance.
    """


class Theme(param.Parameterized):
    """
    Theme objects declare the styles to switch between different color
    modes. Each `Design` may declare any number of color themes.

    `modifiers`
       The modifiers override parameter values of Panel components.
    """

    base_css = param.Filename(doc="""
        A stylesheet declaring the base variables that define the color
        scheme. By default this is inherited from a base class.""")

    bokeh_theme = param.ClassSelector(class_=(_BkTheme, str), default=None, doc="""
        A Bokeh Theme class that declares properties to apply to Bokeh
        models. This is necessary to ensure that plots and other canvas
        based components are styled appropriately.""")

    css = param.Filename(doc="""
       A stylesheet that overrides variables specifically for the
       Theme subclass. In most cases, this is not necessary.""")

    modifiers: t.ClassVar[dict[type[Viewable], dict[str, t.Any]]] = {}


BOKEH_DARK = dict(_dark_minimal.json)
BOKEH_DARK['attrs']['Plot'].update({
    "background_fill_color": "#2b3035",
    "border_fill_color": "#212529",
})

THEME_CSS = pathlib.Path(__file__).parent / 'css'


class DefaultTheme(Theme):
    """
    Baseclass for default or light themes.
    """

    base_css = param.Filename(default=THEME_CSS / 'default.css')

    _name: t.ClassVar[str] = 'default'


class DarkTheme(Theme):
    """
    Baseclass for dark themes.
    """

    base_css = param.Filename(default=THEME_CSS / 'dark.css')

    bokeh_theme = param.ClassSelector(class_=(_BkTheme, str),
                                      default=_BkTheme(json=BOKEH_DARK))

    _name: t.ClassVar[str] = 'dark'


class Design(param.Parameterized, ResourceComponent):

    theme = param.ClassSelector(class_=Theme)

    # Defines parameter overrides to apply to each model
    modifiers: t.ClassVar[dict[type[Viewable], dict[str, t.Any]]] = {}

    # Defines the resources required to render this theme
    _resources = {}

    # Declares valid themes for this Design
    _themes: t.ClassVar[dict[str, type[Theme]]] = {
        'default': DefaultTheme,
        'dark': DarkTheme
    }

    _cache: t.ClassVar[dict[str, ImportedStyleSheet]] = {}

    def __init__(self, theme=None, **params):
        if isinstance(theme, type) and issubclass(theme, Theme):
            theme = theme._name
        elif theme is None:
            theme = 'default'
        theme = self._themes[theme]()
        super().__init__(theme=theme, **params)

    #----------------------------------------------------------------
    # Theme extraction helpers
    #----------------------------------------------------------------

    @staticmethod
    def _extract_css_variables_from_text(css_text: str) -> dict[str, str]:
        variables: dict[str, str] = {}
        pattern = re.compile(r'(--[\w-]+)\s*:\s*([^;]+);?')
        for match in pattern.finditer(css_text):
            name = match.group(1)
            value = match.group(2).strip()
            variables[name] = value
        return variables

    def get_css_variables(self) -> dict[str, str]:
        """
        Extracts CSS custom properties (variables) from the current theme's
        base_css and css files.

        Returns
        -------
        Dict mapping CSS variable names to their values.
        """
        variables: dict[str, str] = {}
        theme = self.theme
        if theme is None:
            return variables
        for attr in ('base_css', 'css'):
            css_path = getattr(theme, attr, None)
            if css_path is None:
                continue
            css_file = pathlib.Path(css_path)
            if css_file.is_file():
                try:
                    css_text = css_file.read_text(encoding='utf-8')
                    variables.update(self._extract_css_variables_from_text(css_text))
                except Exception:
                    pass
        return variables

    def get_base_css_text(self) -> str:
        """
        Returns the base CSS text for the current theme.
        """
        theme = self.theme
        if theme is None or theme.base_css is None:
            return ''
        css_file = pathlib.Path(theme.base_css)
        if css_file.is_file():
            try:
                return css_file.read_text(encoding='utf-8')
            except Exception:
                return ''
        return ''

    def get_theme_css_text(self) -> str:
        """
        Returns the theme-specific CSS text (not the base) for the current theme.
        """
        theme = self.theme
        if theme is None or theme.css is None:
            return ''
        css_file = pathlib.Path(theme.css)
        if css_file.is_file():
            try:
                return css_file.read_text(encoding='utf-8')
            except Exception:
                return ''
        return ''

    def is_dark_theme(self) -> bool:
        """
        Returns whether the current theme is a dark theme.
        """
        return isinstance(self.theme, DarkTheme)

    def get_design_name(self) -> str:
        """
        Returns the lowercase name of this design (e.g. 'fast', 'bootstrap').
        """
        return type(self).__name__.lower()

    def get_theme_name(self) -> str:
        """
        Returns the short name of the current theme ('default' or 'dark').
        """
        theme = self.theme
        if theme is None:
            return 'default'
        return getattr(theme, '_name', 'default')

    def get_bokeh_theme_json(self) -> dict[str, t.Any]:
        """
        Returns a JSON-serializable dict representation of the Bokeh theme.
        """
        bokeh_theme = getattr(self.theme, 'bokeh_theme', None)
        if bokeh_theme is None:
            return {}
        if isinstance(bokeh_theme, str):
            bokeh_theme_obj = built_in_themes.get(bokeh_theme)
            if bokeh_theme_obj is not None:
                return dict(bokeh_theme_obj._json)
            return {}
        return dict(bokeh_theme._json)

    #----------------------------------------------------------------
    # Runtime theme switching
    #----------------------------------------------------------------

    def set_theme(self, theme: t.Union[str, type[Theme], Theme]) -> None:
        """
        Switches the theme at runtime. Updates the internal theme and
        reapplies it to all components that are using this Design.

        Parameters
        ----------
        theme : str, Theme class, or Theme instance
            - If a string, must be a key in the Design's _themes dict
              (e.g. 'default', 'dark').
            - If a class, must be a subclass of Theme.
            - If an instance, must be an instance of Theme.
        """
        if isinstance(theme, Theme):
            new_theme = theme
        elif isinstance(theme, type) and issubclass(theme, Theme):
            new_theme = theme()
        elif isinstance(theme, str):
            if theme not in self._themes:
                raise ValueError(
                    f"Theme '{theme}' is not valid for {type(self).__name__}. "
                    f"Valid themes: {list(self._themes.keys())}"
                )
            new_theme = self._themes[theme]()
        else:
            raise TypeError(
                f"theme must be str, Theme class, or Theme instance, got {type(theme).__name__}"
            )

        with param.edit_constant(self):
            self.theme = new_theme

        self._reapply_to_active_documents()

    def _reapply_to_active_documents(self) -> None:
        """
        Reapplies the design with the new theme to all active documents
        where this design is being used.
        """
        from ..io.state import state

        for doc, template in state._templates.items():
            if not hasattr(template, '_design') or template._design is not self:
                continue
            self._reapply_to_document(doc, template)

    def _reapply_to_document(self, doc: Document, template) -> None:
        """
        Reapplies the design with the new theme to a specific document.
        """
        from ..io.state import state

        if doc in state._stylesheets:
            cache = state._stylesheets[doc]
        else:
            state._stylesheets[doc] = cache = {}

        if self.theme and self.theme.bokeh_theme and doc:
            doc.theme = self.theme.bokeh_theme

        for ref, (root_view, root_model, view_doc, comm) in list(state._views.items()):
            if view_doc is not doc:
                continue
            if ref in state._fake_roots:
                continue
            with doc.models.freeze():
                try:
                    self._reapply(root_view, root_model, isolated=False, cache=cache, document=doc)
                except Exception:
                    pass

        self._sync_extension_component_themes(doc)

        theme_manager = getattr(template, '_theme_manager', None)
        if theme_manager is not None:
            self._sync_theme_manager(theme_manager)

    def _sync_theme_manager(self, theme_manager_model) -> None:
        """
        Syncs the current design/theme state to a ThemeManager model
        that will propagate changes to the frontend.
        """
        try:
            theme_manager_model.design = self.get_design_name()
            theme_manager_model.theme = self.get_theme_name()
            theme_manager_model.theme_name = type(self.theme).__name__ if self.theme else ''
            theme_manager_model.base_css = self.get_base_css_text()
            theme_manager_model.theme_css = self.get_theme_css_text()
            theme_manager_model.css_variables = self.get_css_variables()
            theme_manager_model.design_name = type(self).__name__
            theme_manager_model.is_dark = self.is_dark_theme()
            theme_manager_model.bokeh_theme_json = self.get_bokeh_theme_json()
            theme_manager_model.resources = self.get_resources()
            theme_manager_model.extension_themes = self.get_extension_themes()
            theme_manager_model.fast_style = self.get_fast_style_dict()
            theme_manager_model.bs_theme = self.get_bs_theme()
        except Exception:
            pass

    #----------------------------------------------------------------
    # Runtime Design switching helpers
    #----------------------------------------------------------------

    def get_resources(self) -> dict[str, dict[str, str]]:
        """
        Returns a dict of design resources grouped by type:
        'css', 'js', 'js_modules', 'font'. Each maps a resource name
        to its URL/path. Only resources that can be dynamically
        injected at runtime are included (css and font).
        """
        result: dict[str, dict[str, str]] = {}
        resources = getattr(type(self), '_resources', {})
        for rtype in ('css', 'font'):
            if rtype in resources:
                result[rtype] = dict(resources[rtype])
        return result

    def get_extension_themes(self) -> dict[str, t.Any]:
        """
        Extracts theme configuration for extension components
        (Tabulator, ECharts, Plotly) from the design modifiers.
        """
        from ..widgets import Tabulator
        ext: dict[str, t.Any] = {}
        modifiers = {}
        for scls in type(self).__mro__[::-1]:
            modifiers.update(getattr(scls, 'modifiers', {}).get(Tabulator, {}))
        if 'theme' in modifiers:
            ext['tabulator_theme'] = modifiers['theme']
        if 'theme_classes' in modifiers:
            ext['tabulator_theme_classes'] = list(modifiers['theme_classes'])
        ext.setdefault('echarts_theme_light', 'default')
        ext.setdefault('echarts_theme_dark', 'dark')
        ext.setdefault('plotly_template_light', 'plotly_white')
        ext.setdefault('plotly_template_dark', 'plotly_dark')
        return ext

    def get_fast_style_dict(self) -> dict[str, t.Any]:
        """
        Returns Fast Design-specific style parameters.
        Override in Fast subclass.
        """
        return {}

    def get_bs_theme(self) -> str:
        """
        Returns Bootstrap theme attribute ('light' or 'dark').
        Override in Bootstrap subclass.
        """
        return 'dark' if self.is_dark_theme() else 'light'

    def apply_runtime_to_document(self, doc: Document, template) -> None:
        """
        Unified runtime application of this design to a document.
        Applies Bokeh theme, reapplies modifiers to all rendered
        views, syncs extension component (Plotly/ECharts/Tabulator)
        theme parameters via the standard param/model change chain,
        and syncs the ThemeManager.

        Parameters
        ----------
        doc : Document
            The Bokeh document to apply the design to.
        template : BaseTemplate
            The template associated with the document.
        """
        if doc in state._stylesheets:
            cache = state._stylesheets[doc]
        else:
            state._stylesheets[doc] = cache = {}

        if self.theme and self.theme.bokeh_theme and doc:
            doc.theme = self.theme.bokeh_theme

        for ref, (root_view, root_model, view_doc, comm) in list(state._views.items()):
            if view_doc is not doc:
                continue
            if ref in state._fake_roots:
                continue
            with doc.models.freeze():
                try:
                    self._reapply(root_view, root_model, isolated=False, cache=cache, document=doc)
                except Exception:
                    pass

        self._sync_extension_component_themes(doc)

        theme_manager = getattr(template, '_theme_manager', None)
        if theme_manager is not None:
            self._sync_theme_manager(theme_manager)

    def _sync_extension_component_themes(self, doc: Document) -> None:
        """
        Syncs theme parameters of already-rendered extension components
        (Tabulator, ECharts, Plotly) via the standard param/model
        change chain, so that the Python object, Bokeh model and
        frontend rendering always stay consistent.

        This is the authoritative channel for extension theme updates;
        the panel:themechange DOM event is only a fallback notification.
        """
        from ..widgets import Tabulator
        from ..pane.echarts import ECharts as EChartsPane
        from ..pane.plotly import Plotly as PlotlyPane

        ext_themes = self.get_extension_themes()
        is_dark = self.is_dark_theme()
        echarts_theme = ext_themes.get('echarts_theme_dark' if is_dark else 'echarts_theme_light', 'default')
        plotly_template = ext_themes.get('plotly_template_dark' if is_dark else 'plotly_template_light', 'plotly_white')
        tabulator_theme = ext_themes.get('tabulator_theme')
        tabulator_theme_classes = ext_themes.get('tabulator_theme_classes', [])

        for ref, (root_view, root_model, view_doc, comm) in list(state._views.items()):
            if view_doc is not doc:
                continue
            if ref in state._fake_roots:
                continue
            for obj in root_view.select():
                try:
                    if isinstance(obj, Tabulator):
                        if tabulator_theme is not None and obj.theme != tabulator_theme:
                            obj.theme = tabulator_theme
                        if tabulator_theme_classes and list(obj.theme_classes) != list(tabulator_theme_classes):
                            obj.theme_classes = list(tabulator_theme_classes)
                    elif isinstance(obj, EChartsPane):
                        if hasattr(obj, 'theme') and obj.theme != echarts_theme:
                            obj.theme = echarts_theme
                    elif isinstance(obj, PlotlyPane):
                        if hasattr(obj, 'layout') and isinstance(obj.layout, dict):
                            current_template = obj.layout.get('template')
                            if current_template != plotly_template:
                                obj.layout = dict(obj.layout, template=plotly_template)
                except Exception:
                    pass

    def _reapply(
        self, viewable: Viewable, root: Model, old_models: list[Model] | None = None,
        isolated: bool = True, cache=None, document: Document | None = None
    ) -> None:
        ref = root.ref['id']
        seen = set()
        for o in viewable.select():
            if o.design and not isolated:
                continue
            elif not o.design and not isolated:
                o._design = self

            if ref in o._models:
                model = o._models[ref][0]
                if (old_models and model in old_models) or model in seen:
                    continue
                seen.add(model)
            theme = self.theme
            if theme is None:
                continue
            if document:
                # Theme hook may be applied during callback triggered from a different document
                # we must set_curdoc to ensure style caches are not shared across documents
                with set_curdoc(document):
                    self._apply_modifiers(o, ref, theme, isolated, cache, document)
            else:
                self._apply_modifiers(o, ref, theme, isolated, cache, document)

    def _apply_hooks(self, viewable: Viewable, root: Model, changed: Viewable, old_models=None) -> None:
        from ..io.state import state
        if root.document is None:
            cache: dict[str, ImportedStyleSheet] = {}
        elif root.document in state._stylesheets:
            cache = state._stylesheets[root.document]
        else:
            state._stylesheets[root.document] = cache = {}
        if root.document:
            with root.document.models.freeze():
                self._reapply(changed, root, old_models, isolated=False, cache=cache, document=root.document)
        else:
            self._reapply(changed, root, old_models, isolated=False, cache=cache)

    def _wrapper(self, viewable):
        return viewable

    @classmethod
    def _resolve_stylesheets(cls, value, defining_cls, inherited):
        from ..io.resources import resolve_stylesheet
        stylesheets = []
        for stylesheet in value:
            if stylesheet is Inherit:
                stylesheets.extend(inherited)
                continue
            resolved = resolve_stylesheet(defining_cls, stylesheet, 'modifiers')
            if resolved not in stylesheets:
                stylesheets.append(resolved)
        return stylesheets

    @classmethod
    @functools.lru_cache
    def _resolve_modifiers(cls, vtype, theme, is_server=False):
        """
        Iterate over the class hierarchy in reverse order and accumulate
        all modifiers that apply to the objects class and its super classes.
        """
        modifiers, child_modifiers = {}, {}
        for scls in vtype.__mro__[::-1]:
            cls_modifiers = cls.modifiers.get(scls, {})
            modifiers.update(theme.modifiers.get(scls, {}))
            for super_cls in cls.__mro__[::-1]:
                cls_modifiers = getattr(super_cls, 'modifiers', {}).get(scls, {})
                for prop, value in cls_modifiers.items():
                    if prop == 'children':
                        continue
                    elif prop == 'stylesheets':
                        modifiers[prop] = cls._resolve_stylesheets(value, super_cls, modifiers.get(prop, []))
                    else:
                        modifiers[prop] = value
                child_modifiers.update(cls_modifiers.get('children', {}))
        return modifiers, child_modifiers

    @classmethod
    def _get_modifiers(
        cls, viewable: Viewable, theme: Theme | None = None, isolated: bool = True
    ):
        from ..io.resources import (
            CDN_DIST, component_resource_path, resolve_custom_path,
        )
        theme_type = type(theme) if isinstance(theme, Theme) else theme
        is_server = bool(state.curdoc.session_context) if not state._is_pyodide and state.curdoc else False
        modifiers, child_modifiers = cls._resolve_modifiers(type(viewable), theme_type, is_server=is_server)  # type: ignore
        modifiers = dict(modifiers)
        if 'stylesheets' in modifiers:
            if isolated:
                pre = list(cls._resources.get('css', {}).values())
                for p in ('base_css', 'css'):
                    css = getattr(theme, p)
                    if css is None:
                        continue
                    css = pathlib.Path(css)
                    if relative_to(css, THEME_CSS):
                        pre.append(f'{CDN_DIST}bundled/theme/{css.name}')
                    elif resolve_custom_path(theme, css):
                        pre.append(component_resource_path(theme, p, css))
                    else:
                        pre.append(css.read_text(encoding='utf-8'))
            else:
                pre = []
            modifiers['stylesheets'] = pre + modifiers['stylesheets']
        return modifiers, child_modifiers

    @classmethod
    def _patch_modifiers(cls, doc: Document | None, modifiers: dict[str, t.Any], cache: dict[str, ImportedStyleSheet]):
        if 'stylesheets' in modifiers:
            stylesheets = []
            for sts in modifiers['stylesheets']:
                if sts.endswith('.css'):
                    if cache and sts in cache:
                        sts = cache[sts]
                    else:
                        sts = ImportedStyleSheet(url=sts)
                        if cache is not None:
                            cache[sts.url] = sts
                stylesheets.append(sts)
            modifiers['stylesheets'] = stylesheets

    @classmethod
    def _apply_modifiers(
        cls, viewable: Viewable, mref: str, theme: Theme, isolated: bool,
        cache=None, document=None
    ) -> None:
        if mref not in viewable._models:
            return
        if cache is None:
            cache = cls._cache
        model, _ = viewable._models[mref]
        modifiers, child_modifiers = cls._get_modifiers(viewable, theme, isolated)
        cls._patch_modifiers(model.document or document, modifiers, cache)
        if child_modifiers:
            for child in viewable:
                cls._apply_params(child, mref, child_modifiers, document)
        if modifiers:
            cls._apply_params(viewable, mref, modifiers, document)

    @classmethod
    def _apply_params(cls, viewable, mref, modifiers, document=None):
        # Apply params never sync the modifier values with the Viewable
        # This should not be a concern since most `Layoutable` properties,
        # e.g. stylesheets or sizing_mode, are not synced between the
        # Panel component and the model anyway however in certain edge cases
        # this may end up causing issues.
        from ..io.resources import CDN_DIST, patch_stylesheet

        if mref not in viewable._models:
            return
        model, _ = viewable._models[mref]
        params = {
            k: v for k, v in modifiers.items() if k != 'children' and
            getattr(viewable, k) == viewable.param[k].default
        }
        if 'stylesheets' in modifiers:
            params['stylesheets'] = modifiers['stylesheets'] + viewable.stylesheets

        if isinstance(viewable, PyComponent):
            props = viewable._view__._process_param_change(params)
        else:
            props = viewable._process_param_change(params)
        doc = model.document or document
        if doc and 'dist_url' in doc._template_variables:
            dist_url = doc._template_variables['dist_url']
        else:
            dist_url = CDN_DIST
        for stylesheet in props.get('stylesheets', []):
            if isinstance(stylesheet, ImportedStyleSheet):
                patch_stylesheet(stylesheet, dist_url)

        # Do not update stylesheets if they match
        if 'stylesheets' in props and len(model.stylesheets) == len(props['stylesheets']):
            all_match = True
            stylesheets = []
            for st1, st2 in zip(model.stylesheets, props['stylesheets']):
                if st1 == st2:
                    stylesheets.append(st1)
                    continue
                elif type(st1) is type(st2) and isinstance(st1, ImportedStyleSheet) and st1.url == st2.url:
                    stylesheets.append(st1)
                    continue
                stylesheets.append(st2)
                all_match = False
            if all_match:
                del props['stylesheets']
            else:
                props['stylesheets'] = stylesheets
        if props:
            model.update(**props)
        if hasattr(viewable, '_synced_properties') and 'objects' in viewable._property_mapping:
            obj_key = viewable._property_mapping['objects']
            child_props = {
                p: v for p, v in params.items() if p in viewable._synced_properties
            }
            for child in getattr(model, obj_key):
                child.update(**child_props)

    #----------------------------------------------------------------
    # Public API
    #----------------------------------------------------------------

    def apply(self, viewable: Viewable, root: Model, isolated: bool = True):
        """
        Applies the Design to a Viewable and all it children.

        Parameters
        ----------
        viewable: Viewable
            The Viewable to apply the Design to.
        root: Model
            The root Bokeh model to apply the Design to.
        isolated: bool
            Whether the Design is applied to an individual component
            or embedded in a template that ensures the resources,
            such as CSS variable definitions and JS are already
            initialized.
        """
        doc = root.document
        if not doc:
            self._reapply(viewable, root, isolated=isolated)
            return

        from ..io.state import state
        if doc in state._stylesheets:
            cache = state._stylesheets[doc]
        else:
            state._stylesheets[doc] = cache = {}
        with doc.models.freeze():
            self._reapply(viewable, root, isolated=isolated, cache=cache)
            if self.theme and self.theme.bokeh_theme and doc:
                doc.theme = self.theme.bokeh_theme

    def apply_bokeh_theme_to_model(self, model: Model, theme_override=None):
        """
        Applies the Bokeh theme associated with this Design system
        to a model.

        Parameters
        ----------
        model: bokeh.model.Model
            The Model to apply the theme on.
        theme_override: str | None
            A different theme to apply.
        """
        default_theme = self.theme.bokeh_theme if self.theme is not None else None
        theme = theme_override or default_theme
        if isinstance(theme, str):
            theme = built_in_themes.get(theme)
        if not theme:
            return
        for sm in model.references():
            theme.apply_to_model(sm)

    def resolve_resources(
        self,
        cdn: bool | t.Literal['auto'] = 'auto',
        extras: dict[str, dict[str, str]] | None = None,
        include_theme: bool = True
    ) -> ResourceTypes:
        """
        Resolves the resources required for this design component.

        Parameters
        ----------
        cdn: bool | Literal['auto']
            Whether to load resources from CDN or local server. If set
            to 'auto' value will be automatically determine based on
            global settings.
        extras: dict[str, dict[str, str]] | None
            Additional resources to add to the bundle. Valid resource
            types include js, js_modules and css.
        include_theme: bool
            Whether to include theme resources.

        Returns
        -------
        Dictionary containing JS and CSS resources.
        """
        resource_types = super().resolve_resources(cdn=cdn, extras=extras)
        if not include_theme:
            return resource_types
        dist_path = get_dist_path(cdn=cdn)
        version_suffix = f'?v={JS_VERSION}'
        css_files = resource_types['css']
        theme = self.theme
        if theme is None:
            return resource_types
        for attr in ('base_css', 'css'):
            css = getattr(theme, attr, None)
            if css is None:
                continue
            basename = os.path.basename(css)
            key = 'theme_base' if 'base' in attr else 'theme'
            if relative_to(css, THEME_CSS):
                css_files[key] = dist_path + f'bundled/theme/{basename}{version_suffix}'
            elif resolve_custom_path(theme, css):
                owner = type(theme).param[attr].owner
                css_files[key] = component_resource_path(owner, attr, css)
        return resource_types

    def params(
        self, viewable: Viewable, doc: Document | None = None
    ) -> tuple[dict[str, t.Any], dict[str, t.Any]]:
        """
        Provides parameter values to apply the provided Viewable.

        Parameters
        ----------
        viewable: Viewable
            The Viewable to return modifiers for.
        doc: Document | None
            Document the Viewable will be rendered into. Useful
            for caching any stylesheets that are created.

        Returns
        -------
        modifiers: Dict[str, Any]
            Dictionary of parameter values to apply to the Viewable.
        child_modifiers: Dict[str, Any]
            Dictionary of parameter values to apply to the children
            of the Viewable.
        """
        from ..io.state import state
        if doc is None:
            cache = {}
        elif doc in state._stylesheets:
            cache = state._stylesheets[doc]
        else:
            state._stylesheets[doc] = cache = {}
        modifiers, child_modifiers = self._get_modifiers(viewable, theme=self.theme)
        self._patch_modifiers(doc, modifiers, cache)
        return modifiers, child_modifiers


config.param.design.class_ = Design
THEMES = {
    'default': DefaultTheme,
    'dark': DarkTheme
}
