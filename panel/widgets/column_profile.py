from __future__ import annotations

import typing as t

from dataclasses import dataclass, field, asdict

if t.TYPE_CHECKING:
    import pandas as pd
    from bokeh.models import ColumnDataSource

from .table_helpers import (
    SortState,
    FilterState,
    sort_dataframe,
    get_header_filters,
    filter_dataframe,
    compute_max_page,
    get_page_bounds,
)


T = t.TypeVar("T")


@dataclass
class ColumnWidthState:
    field: str
    width: int | None = None


@dataclass
class PaginationState:
    page: int = 1
    page_size: int | None = None
    pagination: t.Literal["local", "remote"] | None = None


@dataclass
class ColumnProfileState:
    column_widths: list[ColumnWidthState] = field(default_factory=list)
    hidden_columns: list[str] = field(default_factory=list)
    sorters: list[SortState] = field(default_factory=list)
    filters: list[FilterState] = field(default_factory=list)
    groupby: list[str] = field(default_factory=list)
    pagination: PaginationState = field(default_factory=PaginationState)

    def to_dict(self) -> dict[str, t.Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, t.Any]) -> "ColumnProfileState":
        pagination_data = data.get("pagination", {})
        return cls(
            column_widths=[
                ColumnWidthState(**cw) for cw in data.get("column_widths", [])
            ],
            hidden_columns=list(data.get("hidden_columns", [])),
            sorters=[SortState(**s) for s in data.get("sorters", [])],
            filters=[FilterState(**f) for f in data.get("filters", [])],
            groupby=list(data.get("groupby", [])),
            pagination=PaginationState(**pagination_data),
        )


# =========================================================================
# ColumnProfile — Tabulator-only state manager
# Wraps model property access, profile lifecycle, and delegates DataFrame
# processing to the stateless helpers in table_helpers.
# =========================================================================


class ColumnProfile:
    def __init__(
        self,
        renamed_cols: dict[str, t.Any] | None = None,
        indexes: list[str] | None = None,
    ):
        self._renamed_cols: dict[str, t.Any] = renamed_cols or {}
        self._indexes: list[str] = indexes or []

    def set_context(
        self,
        renamed_cols: dict[str, t.Any] | None = None,
        indexes: list[str] | None = None,
    ) -> None:
        if renamed_cols is not None:
            self._renamed_cols = renamed_cols
        if indexes is not None:
            self._indexes = indexes

    # --- Unified Model Read Accessors ---

    @staticmethod
    def get_hidden_columns(model: t.Any) -> list[str]:
        return list(getattr(model, "hidden_columns", []))

    @staticmethod
    def get_sorters(model: t.Any) -> list[SortState]:
        result = []
        for s in getattr(model, "sorters", []):
            if isinstance(s, dict):
                result.append(SortState(
                    field=s.get("field", s.get("column", "")),
                    dir=s.get("dir", "asc"),
                ))
            else:
                result.append(s)
        return result

    @staticmethod
    def get_filters(model: t.Any) -> list[FilterState]:
        result = []
        for f in getattr(model, "filters", []):
            if isinstance(f, dict):
                result.append(FilterState(
                    field=f.get("field", ""),
                    type=f.get("type", ""),
                    value=f.get("value"),
                ))
            else:
                result.append(f)
        return result

    @staticmethod
    def get_groupby(model: t.Any) -> list[str]:
        return list(getattr(model, "groupby", []))

    @staticmethod
    def get_page(model: t.Any) -> int:
        return getattr(model, "page", 1)

    @staticmethod
    def get_page_size(model: t.Any) -> int | None:
        return getattr(model, "page_size", None)

    @staticmethod
    def get_pagination_mode(model: t.Any) -> t.Literal["local", "remote"] | None:
        return getattr(model, "pagination", None)

    @staticmethod
    def is_remote_pagination(model: t.Any) -> bool:
        return ColumnProfile.get_pagination_mode(model) == "remote"

    @staticmethod
    def is_local_pagination(model: t.Any) -> bool:
        return ColumnProfile.get_pagination_mode(model) == "local"

    @staticmethod
    def has_pagination(model: t.Any) -> bool:
        return ColumnProfile.get_pagination_mode(model) is not None

    @staticmethod
    def get_max_page(model: t.Any) -> int:
        return getattr(model, "max_page", 0)

    @staticmethod
    def get_profiles(model: t.Any) -> dict[str, t.Any]:
        return dict(getattr(model, "profiles", {}))

    @staticmethod
    def get_active_profile(model: t.Any) -> str | None:
        return getattr(model, "active_profile", None)

    # --- Unified Model Write Accessors ---

    @staticmethod
    def set_hidden_columns(model: t.Any, hidden: list[str]) -> None:
        if hasattr(model, "hidden_columns"):
            model.hidden_columns = list(hidden)

    @staticmethod
    def set_sorters(model: t.Any, sorters: list[SortState] | list[dict[str, t.Any]]) -> None:
        if hasattr(model, "sorters"):
            result = []
            for s in sorters:
                if isinstance(s, dict):
                    result.append({"field": s.get("field", s.get("column", "")), "dir": s.get("dir", "asc")})
                else:
                    result.append(asdict(s))
            model.sorters = result

    @staticmethod
    def set_filters(model: t.Any, filters: list[FilterState] | list[dict[str, t.Any]]) -> None:
        if hasattr(model, "filters"):
            result = []
            for f in filters:
                if isinstance(f, dict):
                    result.append({
                        "field": f.get("field", ""),
                        "type": f.get("type", ""),
                        "value": f.get("value"),
                    })
                else:
                    result.append(asdict(f))
            model.filters = result

    @staticmethod
    def set_groupby(model: t.Any, groupby: list[str]) -> None:
        if hasattr(model, "groupby"):
            model.groupby = list(groupby)

    @staticmethod
    def set_page(model: t.Any, page: int) -> None:
        if hasattr(model, "page"):
            model.page = page

    @staticmethod
    def set_page_size(model: t.Any, page_size: int | None) -> None:
        if hasattr(model, "page_size"):
            model.page_size = page_size

    @staticmethod
    def set_pagination_mode(model: t.Any, mode: t.Literal["local", "remote"] | None) -> None:
        if hasattr(model, "pagination"):
            model.pagination = mode

    @staticmethod
    def set_max_page(model: t.Any, max_page: int) -> None:
        if hasattr(model, "max_page"):
            model.max_page = max_page

    @staticmethod
    def set_profiles(model: t.Any, profiles: dict[str, t.Any]) -> None:
        if hasattr(model, "profiles"):
            model.profiles = dict(profiles)

    @staticmethod
    def set_active_profile(model: t.Any, name: str | None) -> None:
        if hasattr(model, "active_profile"):
            model.active_profile = name

    # --- State <-> Model Conversion ---

    @staticmethod
    def from_model(model: t.Any) -> ColumnProfileState:
        return ColumnProfileState(
            hidden_columns=ColumnProfile.get_hidden_columns(model),
            sorters=ColumnProfile.get_sorters(model),
            filters=ColumnProfile.get_filters(model),
            groupby=ColumnProfile.get_groupby(model),
            pagination=PaginationState(
                page=ColumnProfile.get_page(model),
                page_size=ColumnProfile.get_page_size(model),
                pagination=ColumnProfile.get_pagination_mode(model),
            ),
        )

    @staticmethod
    def apply_to_model(state: ColumnProfileState, model: t.Any) -> None:
        ColumnProfile.set_hidden_columns(model, state.hidden_columns)
        ColumnProfile.set_sorters(model, state.sorters)
        ColumnProfile.set_filters(model, state.filters)
        ColumnProfile.set_groupby(model, state.groupby)
        ColumnProfile.set_pagination_mode(model, state.pagination.pagination)
        ColumnProfile.set_page_size(model, state.pagination.page_size)
        ColumnProfile.set_page(model, state.pagination.page)

    # --- DataFrame Processing (delegate to stateless helpers) ---

    def sort_dataframe(
        self,
        df: pd.DataFrame,
        sorters: list[SortState] | list[dict[str, t.Any]] | None = None,
    ) -> pd.DataFrame:
        return sort_dataframe(df, sorters, self._renamed_cols)

    def get_header_filters(
        self,
        df: pd.DataFrame,
        filters: list[FilterState] | list[dict[str, t.Any]] | None = None,
        header_filters_config: dict[str, t.Any] | None = None,
    ) -> list[pd.Series | np.ndarray]:
        return get_header_filters(df, filters, header_filters_config, self._indexes)

    def filter_dataframe(
        self,
        df: pd.DataFrame,
        header_filters: list[FilterState] | list[dict[str, t.Any]] | None = None,
        internal_filters: list[tuple[str | None, t.Any]] | None = None,
        header_filters_config: dict[str, t.Any] | None = None,
        edited_indexes: list[t.Any] | None = None,
    ) -> pd.DataFrame:
        return filter_dataframe(
            df,
            header_filters=header_filters,
            internal_filters=internal_filters,
            header_filters_config=header_filters_config,
            edited_indexes=edited_indexes,
            indexes=self._indexes,
        )

    # --- Serialization ---

    def serialize(self, state: ColumnProfileState) -> dict[str, t.Any]:
        return state.to_dict()

    def deserialize(self, data: dict[str, t.Any]) -> ColumnProfileState:
        return ColumnProfileState.from_dict(data)

    # --- Pagination (delegate to stateless helpers) ---

    def compute_max_page(self, length: int, page_size: int | None, initial_page_size: int = 20) -> int:
        return compute_max_page(length, page_size, initial_page_size)

    def get_page_bounds(
        self,
        page: int,
        page_size: int | None,
        initial_page_size: int = 20,
    ) -> tuple[int, int]:
        return get_page_bounds(page, page_size, initial_page_size)

    # --- Profile Lifecycle Management ---

    def list_profile_names(self, model: t.Any) -> list[str]:
        return list(self.get_profiles(model).keys())

    def get_profile_state(self, model: t.Any, name: str) -> ColumnProfileState | None:
        profiles = self.get_profiles(model)
        data = profiles.get(name)
        if data is None:
            return None
        try:
            return self.deserialize(data)
        except Exception:
            return None

    def save_profile(self, model: t.Any, name: str | None = None) -> str:
        if name is None:
            active = self.get_active_profile(model)
            if active is not None:
                name = active
            else:
                existing = set(self.list_profile_names(model))
                i = 0
                while f"profile_{i}" in existing:
                    i += 1
                name = f"profile_{i}"
        state = self.from_model(model)
        profiles = self.get_profiles(model)
        profiles[name] = self.serialize(state)
        self.set_profiles(model, profiles)
        self.set_active_profile(model, name)
        return name

    def load_profile(self, model: t.Any, name: str) -> ColumnProfileState | None:
        state = self.get_profile_state(model, name)
        if state is None:
            return None
        self.apply_to_model(state, model)
        self.set_active_profile(model, name)
        return state

    def delete_profile(self, model: t.Any, name: str) -> bool:
        profiles = self.get_profiles(model)
        if name not in profiles:
            return False
        del profiles[name]
        self.set_profiles(model, profiles)
        if self.get_active_profile(model) == name:
            self.set_active_profile(model, None)
        return True

    def switch_profile(self, model: t.Any, name: str | None) -> ColumnProfileState | None:
        if name is None:
            self.set_active_profile(model, None)
            return None
        return self.load_profile(model, name)
