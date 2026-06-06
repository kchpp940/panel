from __future__ import annotations

import typing as t

import param

from ..config import config
from ..io.resources import CDN_DIST, bundled_files
from ..models import GridStack as BkGridStack
from ..reactive import ReactiveHTML
from ..util import classproperty
from .grid import GridSpec

if t.TYPE_CHECKING:
    from collections.abc import Mapping

    from bokeh.model import Model


class GridStack(ReactiveHTML, GridSpec):  # type: ignore[misc, override]
    """
    The `GridStack` layout allows arranging multiple Panel objects in a grid
    using a simple API to assign objects to individual grid cells or to a grid
    span.

    Other layout containers function like lists, but a `GridSpec` has an API
    similar to a 2D array, making it possible to use 2D assignment to populate,
    index, and slice the grid.

    Reference: https://panel.holoviz.org/reference/layouts/GridStack.html

    :Example:

    >>> pn.extension('gridstack')
    >>> gstack = GridStack(sizing_mode='stretch_both')
    >>> gstack[ : , 0: 3] = pn.Spacer(styles=dict(background='red'))
    >>> gstack[0:2, 3: 9] = pn.Spacer(styles=dict(background='green'))
    >>> gstack[2:4, 6:12] = pn.Spacer(styles=dict(background='orange'))
    >>> gstack[4:6, 3:12] = pn.Spacer(styles=dict(background='blue'))
    >>> gstack[0:2, 9:12] = pn.Spacer(styles=dict(background='purple'))
    """

    allow_resize = param.Boolean(default=True, doc="""
        Allow resizing the grid cells.""")

    allow_drag = param.Boolean(default=True, doc="""
        Allow dragging the grid cells.""")

    state: list[dict[str, t.Any]] = param.List(item_type=dict, doc="""
        Current state of the grid (updated as items are resized and
        dragged).""")  # type: ignore[assignment, ty:invalid-assignment]

    width = param.Integer(default=None)

    height = param.Integer(default=None)

    _bokeh_model: t.ClassVar[type[Model]] = BkGridStack

    _extension_name = 'gridstack'

    _template = """
    <div id="grid" class="grid-stack" style="width: 100%; height: 100%">
    {% for key, obj in objects.items() %}
      <div data-id="{{ id(obj) }}" class="grid-stack-item" gs-h="{{ (key[2] or nrows)-(key[0] or 0) }}" gs-w="{{ (key[3] or ncols)-(key[1] or 0) }}" gs-y="{{ (key[0] or 0) }}" gs-x="{{ (key[1] or 0) }}">
        <div id="content" class="grid-stack-item-content">${obj}</div>
      </div>
    {% endfor %}
    </div>
    """ # noqa

    _scripts: t.ClassVar[dict[str, str]] = {}

    __css_raw__ = [
        f'{config.npm_cdn}/gridstack@7.2.3/dist/gridstack.min.css',
        f'{config.npm_cdn}/gridstack@7.2.3/dist/gridstack-extra.min.css'
    ]

    __javascript_raw__ = [
        f'{config.npm_cdn}/gridstack@7.2.3/dist/gridstack-all.js'
    ]

    __js_require__ = {
        'paths': {
            'gridstack': f'{config.npm_cdn}/gridstack@7.2.3/dist/gridstack-all'
        },
        'exports': {
            'gridstack': 'GridStack'
        },
        'shim': {
            'gridstack': {
                'exports': 'GridStack'
            }
        }
    }

    _rename: t.ClassVar[Mapping[str, str | None]] = {
        'nrows': 'nrows', 'ncols': 'ncols', 'objects': 'objects'
    }

    _stylesheets: t.ClassVar[list[str]] = [
        f'{CDN_DIST}css/gridstack.css'
    ]

    @classproperty
    def __js_skip__(cls):
        return {
            'GridStack': cls.__javascript__[0:1],
        }

    @classproperty
    def __javascript__(cls):
        return bundled_files(cls)

    @classproperty
    def __css__(cls):
        return bundled_files(cls, 'css')

    @param.depends('state', watch=True)
    def _update_objects(self):
        if getattr(self, '_updating_objects', False):
            return
        object_ids = {str(id(obj)): obj for obj in self}
        new_objects = {}
        for p in self.state:
            new_objects[(p['y0'], p['x0'], p['y1'], p['x1'])] = object_ids[p['id']]
        self._updating_objects = True
        try:
            current_keys = set(self.objects.keys())
            new_keys = set(new_objects.keys())
            for key in current_keys - new_keys:
                del self.objects[key]
            for key, obj in new_objects.items():
                if key not in self.objects or self.objects[key] is not obj:
                    self.objects[key] = obj
        finally:
            self._updating_objects = False
        self._update_sizing()

    @param.depends('objects', 'ncols', 'nrows', 'width', 'height', 'sizing_mode', watch=True)
    def _update_sizing(self):
        if getattr(self, '_updating_objects', False):
            return
        if self.ncols and self.width:
            width = self.width/self.ncols
        else:
            width = 0

        if self.nrows and self.height:
            height = self.height/self.nrows
        else:
            height = 0

        for (y0, x0, y1, x1), obj in self.objects.items():
            x0 = 0 if x0 is None else x0
            x1 = (self.ncols) if x1 is None else x1
            y0 = 0 if y0 is None else y0
            y1 = (self.nrows) if y1 is None else y1
            h, w = y1-y0, x1-x0

            properties = {}
            if self.sizing_mode in ['fixed', None]:
                if width:
                    properties['width'] = int(w*width)
                if height:
                    properties['height'] = int(h*height)
            else:
                if not obj.sizing_mode:
                    properties['sizing_mode'] = self.sizing_mode
                if self.sizing_mode == 'stretch_both':
                    pass
                elif 'width' in self.sizing_mode and height:
                    properties['height'] = int(h*height)
                elif 'height' in self.sizing_mode and width:
                    properties['width'] = int(w*width)
            obj.param.update(**{
                k: v for k, v in properties.items()
                if not obj.param[k].readonly
            })
