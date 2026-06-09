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
  max_page: number
  profiles: {[key: string]: any}
  active_profile: string | null
}

export interface ProfileEventDispatcher {
  dispatchSave(name: string | null, state: {[key: string]: any}): void
  dispatchLoad(name: string): void
  dispatchDelete(name: string): void
  dispatchSwitch(name: string | null): void
}

export class ColumnProfile {
  private tabulator: any
  private model: TabulatorModelLike
  private dispatcher: ProfileEventDispatcher | null = null
  private _updating_sort: boolean = false
  private _updating_page: boolean = false
  private _updating_page_size: boolean = false
  private _updating_profile: boolean = false

  constructor(tabulator: any, model: TabulatorModelLike) {
    this.tabulator = tabulator
    this.model = model
  }

  setEventDispatcher(dispatcher: ProfileEventDispatcher | null): void {
    this.dispatcher = dispatcher
  }

  updateTabulator(tabulator: any): void {
    this.tabulator = tabulator
  }

  // --- Unified Model Read Accessors ---

  getHiddenColumns(): string[] {
    return [...this.model.hidden_columns]
  }

  getSorters(): SortState[] {
    return [...this.model.sorters]
  }

  getFilters(): FilterState[] {
    return [...this.model.filters]
  }

  getGroupBy(): string[] {
    return [...this.model.groupby]
  }

  getPage(): number {
    return this.model.page
  }

  getPageSize(): number | null {
    return this.model.page_size
  }

  getPaginationMode(): string | null {
    return this.model.pagination
  }

  isRemotePagination(): boolean {
    return this.model.pagination === "remote"
  }

  isLocalPagination(): boolean {
    return this.model.pagination === "local"
  }

  hasPagination(): boolean {
    return this.model.pagination != null
  }

  getMaxPage(): number {
    return this.model.max_page
  }

  getProfiles(): {[key: string]: any} {
    return {...this.model.profiles}
  }

  getActiveProfile(): string | null {
    return this.model.active_profile
  }

  // --- Unified Model Write Accessors ---

  setHiddenColumns(hidden: string[]): void {
    this.model.hidden_columns = [...hidden]
  }

  setSorters(sorters: SortState[]): void {
    this.model.sorters = [...sorters]
  }

  setFilters(filters: FilterState[]): void {
    this.model.filters = [...filters]
  }

  setGroupBy(groupby: string[]): void {
    this.model.groupby = [...groupby]
  }

  setPage(page: number): void {
    this.model.page = page
  }

  setPageSize(pageSize: number | null): void {
    this.model.page_size = pageSize
  }

  setPaginationMode(mode: string | null): void {
    this.model.pagination = mode
  }

  setMaxPage(maxPage: number): void {
    this.model.max_page = maxPage
  }

  setProfiles(profiles: {[key: string]: any}): void {
    this.model.profiles = {...profiles}
  }

  setActiveProfile(name: string | null): void {
    this.model.active_profile = name
  }

  // --- State Collection from Tabulator ---

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
    return this.getGroupBy()
  }

  collectPagination(): PaginationState {
    return {
      page: this.getPage(),
      page_size: this.getPageSize(),
      pagination: (this.getPaginationMode() as PaginationState["pagination"]) ?? null,
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

  // --- State Application to Tabulator ---

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
    if (pageSize != null) {
      this.tabulator.setPageSize(pageSize)
    }
  }

  applyMaxPage(maxPage: number): void {
    if (!this.tabulator) {
      return
    }
    this.tabulator.setMaxPage(maxPage)
    if (this.tabulator.modules.page.pagesElement) {
      this.tabulator.modules.page._setPageButtons()
    }
  }

  applyColumnWidths(widths: ColumnWidthState[]): void {
    if (!this.tabulator) {
      return
    }
    for (const w of widths) {
      const col = this.tabulator.getColumn(w.field)
      if (col && w.width != null) {
        col.setWidth(w.width)
      }
    }
  }

  applyAll(state: Partial<ColumnProfileState>): void {
    if (state.column_widths !== undefined) {
      this.applyColumnWidths(state.column_widths)
    }
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

  applyFromModel(): void {
    // If there is an active profile, apply the FULL serialized state
    // (including column_widths which are not stored as separate model props)
    const activeProfile = this.getActiveProfile()
    if (activeProfile != null) {
      const profileState = this.getProfileState(activeProfile)
      if (profileState != null) {
        this.applyAll(profileState)
        return
      }
    }
    // Otherwise fall back to individual model properties
    this.applyHiddenColumns(this.getHiddenColumns())
    this.applySorters(this.getSorters())
    this.applyFilters(this.getFilters())
    this.applyGroupBy(this.getGroupBy())
    if (this.hasPagination()) {
      this.applyPage(Math.min(this.getMaxPage(), this.getPage()))
      this.applyPageSize(this.getPageSize())
      this.applyMaxPage(this.getMaxPage())
    }
  }

  // --- Sync: Tabulator -> Model ---

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
      this.setHiddenColumns(this.collectHiddenColumns())
      this.setSorters(this.collectSorters())
      this.setFilters(this.collectFilters())
    } finally {
      this._updating_sort = false
      this._updating_page = false
      this._updating_page_size = false
    }
  }

  syncSortersFromTabulatorToModel(): void {
    if (!this.tabulator || this.isRemotePagination()) {
      return
    }
    this._updating_sort = true
    try {
      this.setSorters(this.collectSorters())
    } finally {
      this._updating_sort = false
    }
  }

  syncFiltersFromTabulatorToModel(): void {
    if (!this.tabulator) {
      return
    }
    this.setFilters(this.collectFilters())
  }

  syncPageFromTabulatorToModel(pageno: number): void {
    if (this.isLocalPagination() && this.getPage() !== pageno && !this._updating_page) {
      this._updating_page = true
      try {
        this.setPage(pageno)
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
      this.setSorters(sorts)
      this.setPage(page || 1)
    } finally {
      this._updating_sort = false
      this._updating_page = false
    }
  }

  syncPageSizeToModel(pageSize: number): void {
    this._updating_page_size = true
    try {
      this.setPageSize(Math.max(pageSize || 1, 1))
    } finally {
      this._updating_page_size = false
    }
  }

  // --- Update Guard Flags ---

  isUpdatingSort(): boolean {
    return this._updating_sort
  }

  isUpdatingPage(): boolean {
    return this._updating_page
  }

  isUpdatingPageSize(): boolean {
    return this._updating_page_size
  }

  isUpdatingProfile(): boolean {
    return this._updating_profile
  }

  // --- Profile Lifecycle Management ---

  listProfileNames(): string[] {
    return Object.keys(this.getProfiles())
  }

  getProfileState(name: string): ColumnProfileState | null {
    const profiles = this.getProfiles()
    const data = profiles[name]
    if (!data) {
      return null
    }
    try {
      return this.deserializeProfile(data)
    } catch {
      return null
    }
  }

  saveProfile(name?: string): string {
    if (this._updating_profile) {
      return name ?? this.getActiveProfile() ?? ""
    }
    this._updating_profile = true
    try {
      const state = this.collectAll()
      const serialized = this.serializeProfile(state)
      this.dispatcher?.dispatchSave(name ?? null, serialized)
      return name ?? ""
    } finally {
      this._updating_profile = false
    }
  }

  loadProfile(name: string): ColumnProfileState | null {
    if (this._updating_profile) {
      return null
    }
    this._updating_profile = true
    try {
      this.dispatcher?.dispatchLoad(name)
      return null
    } finally {
      this._updating_profile = false
    }
  }

  deleteProfile(name: string): boolean {
    if (this._updating_profile) {
      return false
    }
    this._updating_profile = true
    try {
      this.dispatcher?.dispatchDelete(name)
      return true
    } finally {
      this._updating_profile = false
    }
  }

  switchProfile(name: string | null): ColumnProfileState | null {
    if (this._updating_profile) {
      return null
    }
    this._updating_profile = true
    try {
      this.dispatcher?.dispatchSwitch(name)
      return null
    } finally {
      this._updating_profile = false
    }
  }

  serializeProfile(state: ColumnProfileState): any {
    return {
      column_widths: state.column_widths,
      hidden_columns: state.hidden_columns,
      sorters: state.sorters,
      filters: state.filters,
      groupby: state.groupby,
      pagination: state.pagination,
    }
  }

  deserializeProfile(data: any): ColumnProfileState {
    return {
      column_widths: (data.column_widths ?? []).map((cw: any) => ({
        field: cw.field,
        width: cw.width ?? null,
      })),
      hidden_columns: [...(data.hidden_columns ?? [])],
      sorters: (data.sorters ?? []).map((s: any) => ({
        field: s.field ?? s.column ?? "",
        dir: s.dir ?? "asc",
      })),
      filters: (data.filters ?? []).map((f: any) => ({
        field: f.field ?? "",
        type: f.type ?? "",
        value: f.value,
      })),
      groupby: [...(data.groupby ?? [])],
      pagination: {
        page: data.pagination?.page ?? 1,
        page_size: data.pagination?.page_size ?? null,
        pagination: data.pagination?.pagination ?? null,
      },
    }
  }

  // --- Formatted Accessors ---

  getFormattedSorters(): any[] {
    const sorters: any[] = []
    const modelSorters = this.getSorters()
    if (modelSorters.length > 0) {
      sorters.push({column: "_index", dir: "asc"})
    }
    for (const sort of [...modelSorters].reverse()) {
      sorters.push({
        column: (sort as any).column ?? sort.field,
        dir: sort.dir,
      })
    }
    return sorters
  }

  getGroupByFunction(): boolean | ((data: any) => string) {
    const groupby = this.getGroupBy()
    const groupFn = (data: any) => {
      const groups: string[] = []
      for (const g of groupby) {
        groups.push(`${g}: ${data[g]}`)
      }
      return groups.join(", ")
    }
    return (groupby.length > 0) ? groupFn : false
  }
}
