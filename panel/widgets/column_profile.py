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

    @staticmethod
    def from_model(model: t.Any) -> ColumnProfileState:
        return ColumnProfileState(
            hidden_columns=list(getattr(model, "hidden_columns", [])),
            sorters=[
                SortState(field=s.get("field", s.get("column", "")), dir=s.get("dir", "asc"))
                for s in getattr(model, "sorters", [])
            ],
            filters=[
                FilterState(
                    field=f.get("field", ""),
                    type=f.get("type", ""),
                    value=f.get("value"),
                )
                for f in getattr(model, "filters", [])
            ],
            groupby=list(getattr(model, "groupby", [])),
            pagination=PaginationState(
                page=getattr(model, "page", 1),
                page_size=getattr(model, "page_size", None),
                pagination=getattr(model, "pagination", None),
            ),
        )

    def apply_to_model(self, state: ColumnProfileState, model: t.Any) -> None:
        if hasattr(model, "hidden_columns"):
            model.hidden_columns = list(state.hidden_columns)
        if hasattr(model, "sorters"):
            model.sorters = [asdict(s) for s in state.sorters]
        if hasattr(model, "filters"):
            model.filters = [asdict(f) for f in state.filters]
        if hasattr(model, "groupby"):
            model.groupby = list(state.groupby)
        if hasattr(model, "page") and state.pagination.page is not None:
            model.page = state.pagination.page
        if hasattr(model, "page_size"):
            model.page_size = state.pagination.page_size

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

    def serialize(self, state: ColumnProfileState) -> dict[str, t.Any]:
        return state.to_dict()

    def deserialize(self, data: dict[str, t.Any]) -> ColumnProfileState:
        return ColumnProfileState.from_dict(data)

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
