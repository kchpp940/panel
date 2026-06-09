from __future__ import annotations

import typing as t

from bokeh.document.document import Document

from ..config import config
from ..io.state import set_curdoc, state
from ..pane import HoloViews
from ..theme.base import THEMES, Design, Theme
from ..theme.native import Native
from ..viewable import Viewable

if t.TYPE_CHECKING:
    from .base import BaseTemplate


class DesignResolver:
    """
    DesignResolver 负责解析和实例化 Design 对象。

    职责：
    - 从 config、模板默认值和显式参数中解析 Design 类
    - 处理跨 Design 判断逻辑（是否需要从 config 继承）
    - 解析 Theme 参数（字符串 -> Theme 类）
    - 实例化 Design 并关联 Theme
    """

    @staticmethod
    def resolve_design_class(
        template_cls: type[BaseTemplate],
        params: dict[str, t.Any],
    ) -> type[Design]:
        """
        解析应该使用的 Design 类。

        优先级：
        1. 用户显式传入的 design 参数
        2. 如果模板默认值为 None/Design/Native，从 config.design 继承
        3. 使用模板自身的默认值
        """
        if 'design' in params:
            return params['design']

        default_design = template_cls.param.design.default
        if (
            default_design in (None, Design, Native)
            and config.design is not None
        ):
            return config.design

        return default_design

    @staticmethod
    def resolve_theme_class(
        theme_param: t.Any,
        fallback_from_config: bool = True,
    ) -> type[Theme]:
        """
        将 theme 参数解析为 Theme 类。

        支持：
        - Theme 类本身
        - 字符串名称（'default', 'dark' 等）
        - None（从 config.theme 获取）
        """
        if theme_param is None and fallback_from_config:
            theme_param = config.theme
        if isinstance(theme_param, str):
            return THEMES[theme_param]
        return theme_param

    @staticmethod
    def resolve_query_theme() -> str | None:
        """
        从 URL 查询参数中解析 theme 值。
        """
        theme_arg = state.session_args.get("theme", None)
        if not theme_arg:
            return None
        theme_arg = theme_arg[0].decode("utf-8")
        return theme_arg.strip("'").strip('"')

    @classmethod
    def instantiate(
        cls,
        design_cls: type[Design],
        theme_cls: type[Theme],
    ) -> Design:
        """
        使用给定的 theme 实例化 Design。
        """
        return design_cls(theme=theme_cls)


class ThemeSynchronizer:
    """
    ThemeSynchronizer 负责将 Design/Theme 同步到全局状态和 Document。

    职责：
    - 将 Bokeh Theme 同步到 Document
    - 将 Design 类型同步到 config.design
    - 管理 Design 应用到 Viewable 树
    """

    @staticmethod
    def sync_to_document(
        design: Design,
        doc: Document,
    ) -> None:
        """
        将 Design 的 bokeh_theme 同步到 Document。
        """
        if design.theme and design.theme.bokeh_theme:
            doc.theme = design.theme.bokeh_theme

    @staticmethod
    def sync_to_config(
        design: Design,
        doc: Document,
    ) -> None:
        """
        将 Design 类型同步到当前文档上下文的 config。
        """
        with set_curdoc(doc):
            config.design = type(design)

    @staticmethod
    def apply_design_to_viewable(
        design: Design,
        viewable: Viewable,
        root_model: t.Any,
        isolated: bool = False,
    ) -> None:
        """
        将 Design 应用到 Viewable 及其子组件。
        """
        design.apply(viewable, root_model, isolated=isolated)

    @staticmethod
    def ensure_hook_registered(
        design: Design,
        viewable: Viewable,
    ) -> None:
        """
        确保 Design 的 _apply_hooks 已注册到 Viewable。
        """
        if design._apply_hooks not in viewable._hooks:
            viewable._hooks.append(design._apply_hooks)


class ComponentThemeUpdater:
    """
    ComponentThemeUpdater 负责更新扩展组件的主题相关参数。

    职责：
    - 同步 Fast 模板参数与 Design.theme.style
    - 同步 Bootstrap 的 html_attrs
    - 更新 HoloViews 等组件的主题
    - 更新模板渲染变量中的主题相关值
    """

    @staticmethod
    def sync_template_params_from_style(
        template: t.Any,
        design: Design,
        override_params: set[str] | None = None,
    ) -> None:
        """
        从 Design.theme.style 同步参数到模板。

        用于 Fast 等具有 style 对象的 Design。
        仅同步模板 param 中存在且用户未显式设置的参数。
        """
        theme = design.theme
        if not hasattr(theme, 'style'):
            return

        style = theme.style
        override_params = override_params or set()
        updates = {}
        for p, v in style.param.values().items():
            if p == 'name':
                continue
            if p in override_params:
                continue
            if p in template.param:
                updates[p] = v
        template.param.update(updates)

    @staticmethod
    def sync_style_from_template_params(
        template: t.Any,
        design: Design,
    ) -> None:
        """
        从模板参数回写到 Design.theme.style。

        在 _update_vars 时调用，确保 style 对象反映最新的模板参数值。
        """
        theme = design.theme
        if not hasattr(theme, 'style'):
            return

        style = theme.style
        updates = {}
        for p in style.param:
            if p == 'name':
                continue
            if p in template.param:
                updates[p] = getattr(template, p)
        style.param.update(updates)

    @staticmethod
    def get_bootstrap_html_attrs(design: Design) -> str:
        """
        获取 Bootstrap 特定的 html 属性（data-bs-theme）。
        """
        theme = design.theme
        if theme is None:
            return ''
        bs_theme = getattr(theme, '_bs_theme', None)
        if bs_theme is None:
            return ''
        return f'data-bs-theme="{bs_theme}"'

    @staticmethod
    def update_holoviews_themes(
        viewables: t.Iterable[Viewable],
        design: Design,
    ) -> None:
        """
        更新 HoloViews 面板的 bokeh_theme。

        在新增渲染项时调用，确保图表组件使用正确的主题。
        """
        if not (design.theme and design.theme.bokeh_theme):
            return

        for obj in viewables:
            for hvpane in obj.select(HoloViews):
                hvpane.theme = design.theme.bokeh_theme

    @staticmethod
    def collect_render_variables(
        template: t.Any,
        design: Design,
        variables: dict[str, t.Any],
    ) -> None:
        """
        收集主题相关的渲染变量。

        子类可扩展，当前收集：
        - theme: theme 对象
        """
        variables['theme'] = design.theme


class SoftReloadService:
    """
    SoftReloadService 负责主题切换时的软重载机制。

    职责：
    - 判断是否需要触发软重载
    - 通过 location.reload 触发页面刷新
    - 保留 URL 中的 theme 参数
    """

    @staticmethod
    def should_reload(
        old_design: Design | None,
        new_design: Design,
    ) -> bool:
        """
        判断 Design 变更是否需要触发软重载。

        当 Design 类型变化或 Theme 变化时，需要软重载以重新加载资源。
        """
        if old_design is None:
            return False
        if type(old_design) is not type(new_design):
            return True
        if type(old_design.theme) is not type(new_design.theme):
            return True
        return False

    @staticmethod
    def trigger_reload(theme_name: str) -> None:
        """
        触发软重载，将 theme 写入 URL 并刷新页面。

        通过设置 location 对象的 reload 标志触发前端刷新。
        """
        for doc, loc in state._locations.items():
            if not doc.session_context:
                continue
            if state._loaded.get(doc):
                loc.reload = True
            else:
                def reload_session(event, loc=loc):
                    loc.reload = True
                doc.on_event('document_ready', reload_session)


class SnapshotStateMigrator:
    """
    SnapshotStateMigrator 负责在主题/Design 切换时迁移状态。

    职责：
    - 在重新应用 Design 前保存当前 Viewable 状态快照
    - 在新 Design 应用后恢复 Viewable 状态
    - 迁移 Document 中的 hooks 和模型引用
    """

    def __init__(self) -> None:
        self._snapshots: dict[str, t.Any] = {}

    def snapshot_viewable_state(
        self,
        viewable: Viewable,
        mref: str,
    ) -> None:
        """
        保存单个 Viewable 的状态快照。

        保存内容：
        - 模型引用（_models）
        - hooks
        - _design 引用
        """
        key = f"{id(viewable)}_{mref}"
        self._snapshots[key] = {
            'models': dict(viewable._models),
            'hooks': list(viewable._hooks),
            'has_design': getattr(viewable, '_design', None) is not None,
        }

    def restore_viewable_state(
        self,
        viewable: Viewable,
        mref: str,
    ) -> None:
        """
        恢复单个 Viewable 的状态快照。
        """
        key = f"{id(viewable)}_{mref}"
        snapshot = self._snapshots.pop(key, None)
        if snapshot is None:
            return

        for ref, model_data in snapshot['models'].items():
            if ref not in viewable._models:
                viewable._models[ref] = model_data

    def snapshot_document(
        self,
        doc: Document,
        template: BaseTemplate,
    ) -> dict[str, t.Any]:
        """
        保存整个模板 Document 的状态快照。

        用于 Design/Theme 切换前保存，切换后恢复。
        """
        snapshot: dict[str, t.Any] = {
            'template_variables': dict(doc._template_variables),
            'theme': doc.theme,
        }
        return snapshot

    def restore_document(
        self,
        doc: Document,
        snapshot: dict[str, t.Any],
    ) -> None:
        """
        恢复 Document 状态。
        """
        for key, value in snapshot.get('template_variables', {}).items():
            doc._template_variables[key] = value

    def migrate_design(
        self,
        old_design: Design | None,
        new_design: Design,
        template: BaseTemplate,
    ) -> None:
        """
        在 Design 切换时执行完整的状态迁移流程。

        1. 保存所有 Viewable 的 hooks 状态
        2. 清理旧 Design 相关的 hooks
        3. 注册新 Design 的 hooks
        """
        if old_design is None:
            return

        for obj, _ in template._render_items.values():
            if old_design._apply_hooks in obj._hooks:
                obj._hooks.remove(old_design._apply_hooks)
            if new_design._apply_hooks not in obj._hooks:
                obj._hooks.append(new_design._apply_hooks)
