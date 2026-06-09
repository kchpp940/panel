from __future__ import annotations

import typing as t

from dataclasses import dataclass, field, asdict

import numpy as np

if t.TYPE_CHECKING:
    import pandas as pd

    from bokeh.models import ColumnDataSource


T = t.TypeVar("T")


@dataclass
class ColumnWidthState:
    field: str
    width: int | None = None


@dataclass
class SortState:
    field: str
    dir: t.Literal["asc", "desc"] = "asc"


@dataclass
class FilterState:
    field: str
    type: str
    value: t.Any = None


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
        ColumnProfile.set_page(model, state.pagination.page)
        ColumnProfile.set_page_size(model, state.pagination.page_size)
        ColumnProfile.set_pagination_mode(model, state.pagination.pagination)

    # --- DataFrame Processing ---

    def sort_dataframe(
        self,
        df: pd.DataFrame,
        sorters: list[SortState] | list[dict[str, t.Any]] | None = None,
    ) -> pd.DataFrame:
        import pandas as pd

        if sorters is None:
            return df

        sorter_states: list[SortState] = []
        for s in sorters:
            if isinstance(s, dict):
                sorter_states.append(SortState(
                    field=s.get("field", s.get("column", "")),
                    dir=s.get("dir", "asc"),
                ))
            else:
                sorter_states.append(s)

        if not sorter_states:
            return df

        fields = [
            self._renamed_cols.get(s.field, s.field) for s in sorter_states
        ]
        ascending = [s.dir == "asc" for s in sorter_states]

        df = df.copy()
        df["_index_"] = np.arange(len(df)).astype(str)
        fields.append("_index_")
        ascending.append(True)

        rename = False
        if "index" in fields and df.index.name is None:
            df.index.name = "index"
            rename = True

        def tabulator_sorter(col: pd.Series) -> pd.Series:
            if col.dtype.kind not in "SUO":
                return col
            try:
                return col.fillna("").str.lower()
            except Exception:
                return col

        df_sorted = df.sort_values(
            fields, ascending=ascending, kind="mergesort", key=tabulator_sorter
        )

        if rename:
            df_sorted.index.name = None
        df_sorted.drop(columns=["_index_"], inplace=True)
        return df_sorted

    def get_header_filters(
        self,
        df: pd.DataFrame,
        filters: list[FilterState] | list[dict[str, t.Any]] | None = None,
        header_filters_config: dict[str, t.Any] | None = None,
    ) -> list[pd.Series | np.ndarray]:
        import pandas as pd

        result: list[pd.Series | np.ndarray] = []
        if filters is None:
            return result

        filter_states: list[FilterState] = []
        for f in filters:
            if isinstance(f, dict):
                filter_states.append(FilterState(
                    field=f.get("field", ""),
                    type=f.get("type", ""),
                    value=f.get("value"),
                ))
            else:
                filter_states.append(f)

        filt_def = header_filters_config or {}

        for filt in filter_states:
            col_name = filt.field
            op = filt.type
            val = filt.value

            if col_name in df.columns:
                col = df[col_name]
            elif col_name in self._indexes:
                if len(self._indexes) == 1:
                    col = df.index
                else:
                    col = df.index.get_level_values(self._indexes.index(col_name))
            else:
                continue

            if isinstance(val, list):
                if len(val) == 1:
                    val = val[0]
                elif not val:
                    continue

            if col.dtype.kind != "O":
                val = col.dtype.type(val)

            if op == "=":
                result.append(col == val)
            elif op == "!=":
                result.append(col != val)
            elif op == "<":
                result.append(col < val)
            elif op == ">":
                result.append(col > val)
            elif op == ">=":
                result.append(col >= val)
            elif op == "<=":
                result.append(col <= val)
            elif op == "in":
                if not isinstance(val, (list, np.ndarray)):
                    val = [val]
                result.append(col.isin(val))
            elif op == "like":
                result.append(col.str.contains(val, case=False, regex=False))
            elif op == "starts":
                result.append(col.str.startswith(val))
            elif op == "ends":
                result.append(col.str.endswith(val))
            elif op == "keywords":
                match_all = filt_def.get(col_name, {}).get("matchAll", False)
                sep = filt_def.get(col_name, {}).get("separator", " ")
                matches = str(val).split(sep)
                if match_all:
                    for match in matches:
                        result.append(col.str.contains(match, case=False, regex=False))
                else:
                    combined = col.str.contains(matches[0], case=False, regex=False)
                    for match in matches[1:]:
                        combined |= col.str.contains(match, case=False, regex=False)
                    result.append(combined)
            elif op == "regex":
                raise ValueError("Regex filtering not supported.")
            else:
                raise ValueError(f"Filter type {op!r} not recognized.")

        return result

    def filter_dataframe(
        self,
        df: pd.DataFrame,
        header_filters: list[FilterState] | list[dict[str, t.Any]] | None = None,
        internal_filters: list[tuple[str | None, t.Any]] | None = None,
        header_filters_config: dict[str, t.Any] | None = None,
        edited_indexes: list[t.Any] | None = None,
    ) -> pd.DataFrame:
        import pandas as pd
        import param
        from functools import partial
        from types import FunctionType, MethodType

        filters: list[pd.Series | np.ndarray] = []

        if internal_filters:
            for col_name, filt in internal_filters:
                if col_name is not None and col_name not in df.columns:
                    continue
                if isinstance(filt, (FunctionType, MethodType, partial)):
                    res = filt(df)
                    if type(res) is type(df):
                        df = res
                    else:
                        filters.append(res)
                    continue
                if isinstance(filt, param.Parameter):
                    if filt.name is None:
                        continue
                    val = getattr(filt.owner, filt.name)
                else:
                    val = filt
                column = df[col_name] if col_name else None
                if val is None:
                    continue
                elif np.isscalar(val):
                    mask = column == val
                elif isinstance(val, (list, set)):
                    if not val:
                        continue
                    mask = column.isin(val)
                elif isinstance(val, tuple):
                    start, end = val
                    if start is None and end is None:
                        continue
                    elif start is None:
                        mask = column <= end
                    elif end is None:
                        mask = column >= start
                    else:
                        mask = (column >= start) & (column <= end)
                else:
                    raise ValueError(
                        f"'{col_name} filter value not understood. Must be either "
                        "a scalar, tuple or list."
                    )
                filters.append(mask)

        if header_filters:
            filters.extend(
                self.get_header_filters(df, header_filters, header_filters_config)
            )

        if filters:
            mask = filters[0]
            for f in filters:
                mask &= f
            if edited_indexes:
                edited_mask = df.index.isin(edited_indexes)
                mask = mask | edited_mask
            df = df[mask]
        return df

    # --- Serialization ---

    def serialize(self, state: ColumnProfileState) -> dict[str, t.Any]:
        return state.to_dict()

    def deserialize(self, data: dict[str, t.Any]) -> ColumnProfileState:
        return ColumnProfileState.from_dict(data)

    # --- Pagination ---

    def compute_max_page(self, length: int, page_size: int | None, initial_page_size: int = 20) -> int:
        nrows = page_size or initial_page_size
        return max(length // nrows + bool(length % nrows), 1)

    def get_page_bounds(
        self,
        page: int,
        page_size: int | None,
        initial_page_size: int = 20,
    ) -> tuple[int, int]:
        nrows = page_size or initial_page_size
        start = (page - 1) * nrows
        end = start + nrows
        return start, end

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
