from __future__ import annotations

import typing as t
from dataclasses import dataclass

import numpy as np

if t.TYPE_CHECKING:
    import pandas as pd
    from bokeh.models import ColumnDataSource


T = t.TypeVar("T")


@dataclass
class SortState:
    field: str
    dir: t.Literal["asc", "desc"] = "asc"


@dataclass
class FilterState:
    field: str
    type: str
    value: t.Any = None


def _normalize_sorters(
    sorters: list[SortState] | list[dict[str, t.Any]] | None,
) -> list[SortState]:
    if not sorters:
        return []
    result: list[SortState] = []
    for s in sorters:
        if isinstance(s, dict):
            result.append(SortState(
                field=s.get("field", s.get("column", "")),
                dir=s.get("dir", "asc"),
            ))
        else:
            result.append(s)
    return result


def _normalize_filters(
    filters: list[FilterState] | list[dict[str, t.Any]] | None,
) -> list[FilterState]:
    if not filters:
        return []
    result: list[FilterState] = []
    for f in filters:
        if isinstance(f, dict):
            result.append(FilterState(
                field=f.get("field", ""),
                type=f.get("type", ""),
                value=f.get("value"),
            ))
        else:
            result.append(f)
    return result


def sort_dataframe(
    df: pd.DataFrame,
    sorters: list[SortState] | list[dict[str, t.Any]] | None,
    renamed_cols: dict[str, t.Any] | None = None,
) -> pd.DataFrame:
    """
    Sort a DataFrame according to Tabulator-style sorter descriptions.
    Pure function: does not mutate ``df`` or any external state.
    """
    import pandas as pd

    sorter_states = _normalize_sorters(sorters)
    if not sorter_states:
        return df

    renamed = renamed_cols or {}
    fields = [renamed.get(s.field, s.field) for s in sorter_states]
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
    df: pd.DataFrame,
    filters: list[FilterState] | list[dict[str, t.Any]] | None,
    header_filters_config: dict[str, t.Any] | None = None,
    indexes: list[str] | None = None,
) -> list[pd.Series | np.ndarray]:
    """
    Build per-column boolean masks from Tabulator-style filter descriptions.
    Pure function: does not mutate ``df`` or any external state.
    """
    import pandas as pd

    result: list[pd.Series | np.ndarray] = []
    filter_states = _normalize_filters(filters)
    if not filter_states:
        return result

    idx_list = indexes or []
    filt_def = header_filters_config or {}

    for filt in filter_states:
        col_name = filt.field
        op = filt.type
        val = filt.value

        if col_name in df.columns:
            col = df[col_name]
        elif col_name in idx_list:
            if len(idx_list) == 1:
                col = df.index
            else:
                col = df.index.get_level_values(idx_list.index(col_name))
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
    df: pd.DataFrame,
    header_filters: list[FilterState] | list[dict[str, t.Any]] | None = None,
    internal_filters: list[tuple[str | None, t.Any]] | None = None,
    header_filters_config: dict[str, t.Any] | None = None,
    edited_indexes: list[t.Any] | None = None,
    indexes: list[str] | None = None,
) -> pd.DataFrame:
    """
    Apply Tabulator header + internal filters to a DataFrame.
    Pure function: does not mutate ``df`` or any external state.
    """
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
            get_header_filters(df, header_filters, header_filters_config, indexes)
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


def compute_max_page(length: int, page_size: int | None, initial_page_size: int = 20) -> int:
    """
    Compute the max page number for a dataset of ``length`` rows.
    Pure function.
    """
    nrows = page_size or initial_page_size
    return max(length // nrows + bool(length % nrows), 1)


def get_page_bounds(
    page: int,
    page_size: int | None,
    initial_page_size: int = 20,
) -> tuple[int, int]:
    """
    Return (start, end) row indices for the given ``page``.
    Pure function.
    """
    nrows = page_size or initial_page_size
    start = (page - 1) * nrows
    end = start + nrows
    return start, end
