"""
Bootstrap template based on the bootstrap.css library.
"""
from __future__ import annotations

import pathlib
import typing as t

import param

from ...theme import Design
from ...theme.bootstrap import Bootstrap
from ..base import BasicTemplate, TemplateActions

_ROOT = pathlib.Path(__file__).parent


class BootstrapTemplateActions(TemplateActions):

    _scripts: t.ClassVar[dict[str, list[str] | str]] = {
        'render': "state.modal = new bootstrap.Modal(document.getElementById('pn-Modal'))",
        'open_modal': "state.modal.show()",
        'close_modal': "state.modal.hide()",
    }


class BootstrapTemplate(BasicTemplate):
    """
    BootstrapTemplate
    """
    design = param.ClassSelector(class_=Design, default=Bootstrap,
                                 is_instance=False, instantiate=False, doc="""
        A Design applies a specific design system to a template.""")

    sidebar_width = param.Integer(default=350, doc="""
        The width of the sidebar in pixels. Default is 350.""")

    _actions = param.ClassSelector(default=BootstrapTemplateActions(), class_=TemplateActions)

    _css = [_ROOT / "bootstrap.css"]

    _template = _ROOT / 'bootstrap.html'

    def _update_vars(self, *args) -> None:
        super()._update_vars(*args)
        self._render_variables['html_attrs'] = self.component_theme_updater.get_bootstrap_html_attrs(self._design)
