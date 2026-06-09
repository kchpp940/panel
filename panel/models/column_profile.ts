export interface ColumnWidthState {
  field: string
  width: number | null
}

export interface ColumnVisibilityState {
  field: string
  visible: boolean
}

export interface SortState {
  field: string
  dir: "asc" | "desc"
}

export interface FilterState {
  field: string
  type: string
  value: any
}

export interface PaginationState {
  page: number
  page_size: number | null
  pagination: "local" | "remote" | null
}

export interface ColumnProfileState {
  column_widths: ColumnWidthState[]
  hidden_columns: string[]
  sorters: SortState[]
  filters: FilterState[]
  groupby: string[]
  pagination: PaginationState
}

export interface TabulatorModelLike {
  hidden_columns: string[]
  sorters: SortState[]
  filters: FilterState[]
  groupby: string[]
  page: number
  page_size: number | null
  pagination: string | null
}

export class ColumnProfile {
  private tabulator: any
  private model: TabulatorModelLike
  private _updating_sort: boolean = false
  private _updating_page: boolean = false
  private _updating_page_size: boolean = false

  constructor(tabulator: any, model: TabulatorModelLike) {
    this.tabulator = tabulator
    this.model = model
  }

  updateTabulator(tabulator: any): void {
    this.tabulator = tabulator
  }

  collectColumnWidths(): ColumnWidthState[] {
    const widths: ColumnWidthState[] = []
    if (!this.tabulator) {
      return widths
    }
    for (const column of this.tabulator.getColumns()) {
      const col = column._column
      if (col.field === "_index") {
        continue
      }
      widths.push({
        field: col.field,
        width: col.width ?? null,
      })
    }
    return widths
  }

  collectHiddenColumns(): string[] {
    const hidden: string[] = []
    if (!this.tabulator) {
      return hidden
    }
    for (const column of this.tabulator.getColumns()) {
      const col = column._column
      if (col.field === "_index") {
        continue
      }
      if (!column.isVisible()) {
        hidden.push(col.field)
      }
    }
    return hidden
  }

  collectSorters(): SortState[] {
    if (!this.tabulator) {
      return []
    }
    const sorters = this.tabulator.getSorters() ?? []
    const result: SortState[] = []
    for (const s of sorters) {
      if (s.field !== "_index") {
        result.push({field: s.field, dir: s.dir})
      }
    }
    return result.reverse()
  }

  collectFilters(): FilterState[] {
    if (!this.tabulator) {
      return []
    }
    return this.tabulator.getFilters() ?? []
  }

  collectGroupBy(): string[] {
    return [...this.model.groupby]
  }

  collectPagination(): PaginationState {
    return {
      page: this.model.page,
      page_size: this.model.page_size,
      pagination: (this.model.pagination as PaginationState["pagination"]) ?? null,
    }
  }

  collectAll(): ColumnProfileState {
    return {
      column_widths: this.collectColumnWidths(),
      hidden_columns: this.collectHiddenColumns(),
      sorters: this.collectSorters(),
      filters: this.collectFilters(),
      groupby: this.collectGroupBy(),
      pagination: this.collectPagination(),
    }
  }

  applyHiddenColumns(hidden: string[]): void {
    if (!this.tabulator) {
      return
    }
    for (const column of this.tabulator.getColumns()) {
      const col = column._column
      if (col.field === "_index") {
        column.hide()
      } else if (hidden.includes(col.field)) {
        column.hide()
      } else {
        column.show()
      }
    }
  }

  applySorters(sorters: SortState[]): void {
    if (!this.tabulator || this._updating_sort) {
      return
    }
    const formatted: any[] = []
    if (sorters.length > 0) {
      formatted.push({column: "_index", dir: "asc"})
    }
    for (const sort of [...sorters].reverse()) {
      formatted.push({column: sort.field, dir: sort.dir})
    }
    this.tabulator.setSort(formatted)
  }

  applyFilters(filters: FilterState[]): void {
    if (!this.tabulator) {
      return
    }
    this.tabulator.setFilter(filters)
  }

  applyGroupBy(groupby: string[]): void {
    if (!this.tabulator) {
      return
    }
    if (groupby.length === 0) {
      this.tabulator.setGroupBy(false)
      return
    }
    const groupFn = (data: any) => {
      const groups: string[] = []
      for (const g of groupby) {
        groups.push(`${g}: ${data[g]}`)
      }
      return groups.join(", ")
    }
    this.tabulator.setGroupBy(groupFn)
  }

  applyPage(page: number): void {
    if (!this.tabulator || this._updating_page) {
      return
    }
    this.tabulator.setPage(page)
  }

  applyPageSize(pageSize: number | null): void {
    if (!this.tabulator) {
      return
    }
    if (!this._updating_page_size) {
    }
    if (pageSize != null) {
      this.tabulator.setPageSize(pageSize)
    }
  }

  applyAll(state: Partial<ColumnProfileState>): void {
    if (state.hidden_columns !== undefined) {
      this.applyHiddenColumns(state.hidden_columns)
    }
    if (state.sorters !== undefined) {
      this.applySorters(state.sorters)
    }
    if (state.filters !== undefined) {
      this.applyFilters(state.filters)
    }
    if (state.groupby !== undefined) {
      this.applyGroupBy(state.groupby)
    }
    if (state.pagination?.page !== undefined) {
      this.applyPage(state.pagination.page)
    }
    if (state.pagination?.page_size !== undefined) {
      this.applyPageSize(state.pagination.page_size)
    }
  }

  syncFromTabulatorToModel(updatingFlags?: {
    sort?: boolean
    page?: boolean
    page_size?: boolean
  }): void {
    if (updatingFlags?.sort !== undefined) {
      this._updating_sort = updatingFlags.sort
    }
    if (updatingFlags?.page !== undefined) {
      this._updating_page = updatingFlags.page
    }
    if (updatingFlags?.page_size !== undefined) {
      this._updating_page_size = updatingFlags.page_size
    }
    try {
      this.model.hidden_columns = this.collectHiddenColumns()
      this.model.sorters = this.collectSorters()
      this.model.filters = this.collectFilters()
    } finally {
      this._updating_sort = false
      this._updating_page = false
      this._updating_page_size = false
    }
  }

  syncSortersFromTabulatorToModel(): void {
    if (!this.tabulator) {
      return
    }
    this._updating_sort = true
    try {
      this.model.sorters = this.collectSorters()
    } finally {
      this._updating_sort = false
    }
  }

  syncFiltersFromTabulatorToModel(): void {
    if (!this.tabulator) {
      return
    }
    this.model.filters = this.collectFilters()
  }

  syncPageFromTabulatorToModel(pageno: number): void {
    if (this.model.pagination === "local" && this.model.page !== pageno && !this._updating_page) {
      this._updating_page = true
      try {
        this.model.page = pageno
      } finally {
        this._updating_page = false
      }
    }
  }

  syncRemotePaginationToModel(page: number, sorters: any[]): void {
    this._updating_sort = true
    this._updating_page = true
    try {
      const sorts: SortState[] = []
      for (const s of sorters ?? []) {
        if (s.field !== "_index") {
          sorts.push({field: s.field, dir: s.dir})
        }
      }
      this.model.sorters = sorts
      this.model.page = page || 1
    } finally {
      this._updating_sort = false
      this._updating_page = false
    }
  }

  syncPageSizeToModel(pageSize: number): void {
    this._updating_page_size = true
    try {
      this.model.page_size = Math.max(pageSize || 1, 1)
    } finally {
      this._updating_page_size = false
    }
  }

  isUpdatingSort(): boolean {
    return this._updating_sort
  }

  isUpdatingPage(): boolean {
    return this._updating_page
  }

  isUpdatingPageSize(): boolean {
    return this._updating_page_size
  }

  getFormattedSorters(): any[] {
    const sorters: any[] = []
    if (this.model.sorters.length > 0) {
      sorters.push({column: "_index", dir: "asc"})
    }
    for (const sort of [...this.model.sorters].reverse()) {
      sorters.push({
        column: (sort as any).column ?? sort.field,
        dir: sort.dir,
      })
    }
    return sorters
  }
}
