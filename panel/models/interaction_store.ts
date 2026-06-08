import type {Attrs} from "@bokehjs/core/types"
import type * as p from "@bokehjs/core/properties"
import {ModelEvent} from "@bokehjs/core/bokeh_events"
import {Model} from "@bokehjs/model"
import {View} from "@bokehjs/core/view"

export type InteractionEventKind = "hover" | "selection" | "viewport" | "row_selection"

export type InteractionFilterOp =
  | "in"
  | "not_in"
  | "range"
  | "=="
  | "!="
  | ">"
  | ">="
  | "<"
  | "<="

export interface InteractionFilter {
  /** Column/field name, or the literal "index" for row-index-based filters. */
  field: string
  /** The filter operation. */
  op: InteractionFilterOp
  /**
   * Value for the filter:
   *  - for "in" / "not_in": an array of values
   *  - for "range": a two-element tuple [min, max], inclusive
   *  - for comparison ops: a scalar value
   */
  value: any
}

export interface InteractionSelectionPoint {
  mode: "point"
  dataset_id?: string
  indices: number[]
  fields: string[]
  values: Array<{[field: string]: any}>
  filters: InteractionFilter[]
}

export interface InteractionSelectionRange {
  mode: "range"
  dataset_id?: string
  ranges: {[axis: string]: [any, any]}
  fields: string[]
  indices?: number[]
  values?: Array<{[field: string]: any}>
  filters: InteractionFilter[]
}

export interface InteractionSelectionRows {
  mode: "rows"
  dataset_id?: string
  indices: number[]
  fields: string[]
  values: Array<{[field: string]: any}>
  filters: InteractionFilter[]
}

export type InteractionSelection =
  | InteractionSelectionPoint
  | InteractionSelectionRange
  | InteractionSelectionRows

export interface InteractionViewport {
  dataset_id?: string
  fields: string[]
  ranges: {[axis: string]: [any, any]}
  filters: InteractionFilter[]
}

export interface InteractionEventPayload {
  kind: InteractionEventKind
  source: string
  source_id: string
  timestamp: number
  payload: any
  selection?: InteractionSelection
  viewport?: InteractionViewport
}

/**
 * Build a list of point filters from a list of row records.
 *
 * `allowed_fields` controls which payload fields get turned into business-data `in` filters.
 * When `allowed_fields` is null/empty, only the `{field: "index", op: "in", value: indices}
 * index filter is emitted (if indices are provided), to avoid guessing business conditions from
 * display/internal/formatting payload keys leaking into the filter.
 */
export function build_point_filters(
  values: Array<{[field: string]: any}>,
  indices: number[] | null = null,
  allowed_fields: string[] | null = null,
): InteractionFilter[] {
  const filters: InteractionFilter[] = []
  if (indices != null && indices.length > 0) {
    filters.push({field: "index", op: "in", value: indices.slice()})
  }
  if (allowed_fields == null || allowed_fields.length === 0) {
    return filters
  }
  const allowedSet = new Set(allowed_fields)
  const fieldValues = new Map<string, Set<any>>()
  for (const row of values) {
    for (const [f, v] of Object.entries(row)) {
      if (!allowedSet.has(f)) continue
      if (v === undefined || v === null) continue
      if (!fieldValues.has(f)) fieldValues.set(f, new Set())
      fieldValues.get(f)!.add(v)
    }
  }
  for (const [field, set] of fieldValues.entries()) {
    filters.push({field, op: "in", value: Array.from(set)})
  }
  return filters
}

/**
 * Build a list of range filters from an axis→[min,max] mapping.
 *
 * When `allowed_fields` controls which axes get normalized into business-data `range` filters.
 * axis names are normalized (e.g. `xaxis.range` → `x`) then filtered by the allowlist;
 * when empty/null, range axis range is allowed filters only if `range filters are emitted.
 */
export function build_range_filters(
  ranges: {[axis: string]: [any, any]},
  allowed_fields: string[] | null = null,
): InteractionFilter[] {
  const filters: InteractionFilter[] = []
  const allowedSet = allowed_fields != null && allowed_fields.length > 0 ? new Set(allowed_fields) : null
  for (const [axis, range] of Object.entries(ranges)) {
    if (!Array.isArray(range) || range.length !== 2) continue
    let field = axis
    const match = axis.match(/^(x|y|xaxis|yaxis)(\d*)\.range$/)
    if (match) {
      const base = match[1].replace("axis", "")
      field = base + (match[2] || "")
    }
    if (allowedSet != null && !allowedSet.has(field)) continue
    filters.push({field, op: "range", value: range})
  }
  return filters
}

/**
 * Given a list of row records and optional column name hints, collect
 * all distinct field names.
 *
 * When `allowed_fields` is provided, the union of hints and value keys is
 * intersected with the allowlist; the allowlist is then returned deduplicated.
 * When `allowed_fields` is null/empty, the union of hints and actual value keys
 * is returned.
 */
export function collect_fields(
  values: Array<{[field: string]: any}>,
  hints: string[] = [],
  allowed_fields: string[] | null = null,
): string[] {
  if (allowed_fields != null && allowed_fields.length > 0) {
    return Array.from(new Set(allowed_fields))
  }
  const fields = new Set<string>(hints)
  for (const row of values) {
    for (const f of Object.keys(row)) fields.add(f)
  }
  return Array.from(fields)
}

export class InteractionEvent extends ModelEvent {
  constructor(
    readonly kind: InteractionEventKind,
    readonly source: string,
    readonly source_id: string,
    readonly payload: any,
    readonly selection?: InteractionSelection,
    readonly viewport?: InteractionViewport,
  ) {
    super()
  }

  protected override get event_values(): Attrs {
    return {
      model: this.origin,
      kind: this.kind,
      source: this.source,
      source_id: this.source_id,
      payload: this.payload,
      selection: this.selection,
      viewport: this.viewport,
    }
  }

  static {
    this.prototype.event_name = "interaction_event"
  }
}

export class InteractionStoreView extends View {
  declare model: InteractionStore
}

export namespace InteractionStore {
  export type Attrs = p.AttrsOf<Props>

  export type Props = Model.Props & {
    events: p.Property<InteractionEventPayload[]>
    hover_events: p.Property<InteractionEventPayload[]>
    selection_events: p.Property<InteractionEventPayload[]>
    viewport_events: p.Property<InteractionEventPayload[]>
    row_selection_events: p.Property<InteractionEventPayload[]>
    sources: p.Property<{[key: string]: string}>
    max_history: p.Property<number>
    _autoclear: p.Property<boolean>
  }
}

export interface InteractionStore extends InteractionStore.Attrs {}

export class InteractionStore extends Model {
  declare properties: InteractionStore.Props

  constructor(attrs?: Partial<InteractionStore.Attrs>) {
    super(attrs)
  }

  static override __module__ = "panel.models.interaction_store"

  static {
    this.prototype.default_view = InteractionStoreView

    this.define<InteractionStore.Props>(({Any, Bool, Int, List, Str}) => ({
      events: [ List(Any), [] ],
      hover_events: [ List(Any), [] ],
      selection_events: [ List(Any), [] ],
      viewport_events: [ List(Any), [] ],
      row_selection_events: [ List(Any), [] ],
      sources: [ Any, {} ],
      max_history: [ Int, 100 ],
      _autoclear: [ Bool, false ],
    }))
  }

  publish_event(event: Omit<InteractionEventPayload, "timestamp">): InteractionEventPayload {
    const full: InteractionEventPayload = {
      ...event,
      timestamp: Date.now(),
    }

    const events = [...this.events, full]
    const overflow = events.length - this.max_history
    const trimmed = overflow > 0 ? events.slice(overflow) : events

    this.events = trimmed
    this._update_filtered_views(trimmed)

    if (event.source != null && this.sources[event.source_id] == null) {
      this.sources = {...this.sources, [event.source_id]: event.source}
    }

    this.trigger_event(new InteractionEvent(
      full.kind, full.source, full.source_id,
      full.payload, full.selection, full.viewport,
    ))

    if (this._autoclear) {
      setTimeout(() => this.clear(full.kind, full.source_id), 0)
    }

    return full
  }

  subscribe(
    callback: (event: InteractionEventPayload) => void,
    kind_filter?: InteractionEventKind | InteractionEventKind[],
    source_filter?: string | string[],
  ): () => void {
    const kinds = kind_filter == null
      ? null
      : Array.isArray(kind_filter) ? new Set(kind_filter) : new Set([kind_filter])
    const sources = source_filter == null
      ? null
      : Array.isArray(source_filter) ? new Set(source_filter) : new Set([source_filter])

    const handler = () => {
      const latest = this.events[this.events.length - 1]
      if (latest == null) {
        return
      }
      if (kinds != null && !kinds.has(latest.kind)) {
        return
      }
      if (sources != null && !sources.has(latest.source_id)) {
        return
      }
      callback(latest)
    }

    this.on_change(this.properties.events, handler)

    return () => {
      this.remove_on_change(this.properties.events, handler)
    }
  }

  clear(kind?: InteractionEventKind, source_id?: string): void {
    let filtered = this.events
    if (kind != null) {
      filtered = filtered.filter((e) => e.kind !== kind)
    }
    if (source_id != null) {
      filtered = filtered.filter((e) => e.source_id !== source_id)
    }
    this.events = filtered
    this._update_filtered_views(filtered)
  }

  clear_all(): void {
    this.events = []
    this._update_filtered_views([])
  }

  get_events(
    kind?: InteractionEventKind,
    source_id?: string,
  ): InteractionEventPayload[] {
    return this.events.filter((e) => {
      if (kind != null && e.kind !== kind) {
        return false
      }
      if (source_id != null && e.source_id !== source_id) {
        return false
      }
      return true
    })
  }

  _update_filtered_views(events: InteractionEventPayload[]): void {
    const hover: InteractionEventPayload[] = []
    const selection: InteractionEventPayload[] = []
    const viewport: InteractionEventPayload[] = []
    const row_selection: InteractionEventPayload[] = []

    for (const e of events) {
      switch (e.kind) {
        case "hover":
          hover.push(e)
          break
        case "selection":
          selection.push(e)
          break
        case "viewport":
          viewport.push(e)
          break
        case "row_selection":
          row_selection.push(e)
          break
      }
    }

    this.hover_events = hover
    this.selection_events = selection
    this.viewport_events = viewport
    this.row_selection_events = row_selection
  }
}
