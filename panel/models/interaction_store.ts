import type {Attrs} from "@bokehjs/core/types"
import type * as p from "@bokehjs/core/properties"
import {ModelEvent} from "@bokehjs/core/bokeh_events"
import {Model} from "@bokehjs/model"
import {View} from "@bokehjs/core/view"

export type InteractionEventType = "hover" | "selection" | "viewport" | "row_selection"

export interface InteractionEventPayload {
  type: InteractionEventType
  source: string
  data: any
  timestamp: number
}

export class InteractionEvent extends ModelEvent {
  constructor(
    readonly type: InteractionEventType,
    readonly source: string,
    readonly data: any,
  ) {
    super()
  }

  protected override get event_values(): Attrs {
    return {model: this.origin, type: this.type, source: this.source, data: this.data}
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

  publish(
    type: InteractionEventType,
    source: string,
    source_name: string | null,
    data: any,
  ): InteractionEventPayload {
    const event: InteractionEventPayload = {
      type,
      source,
      data,
      timestamp: Date.now(),
    }

    const events = [...this.events, event]
    const overflow = events.length - this.max_history
    const trimmed = overflow > 0 ? events.slice(overflow) : events

    this.events = trimmed
    this._update_filtered_views(trimmed)

    if (source_name != null && this.sources[source] == null) {
      this.sources = {...this.sources, [source]: source_name}
    }

    this.trigger_event(new InteractionEvent(type, source, data))

    if (this._autoclear) {
      setTimeout(() => this.clear_by_source(source, type), 0)
    }

    return event
  }

  subscribe(
    callback: (event: InteractionEventPayload) => void,
    type_filter?: InteractionEventType | InteractionEventType[],
    source_filter?: string | string[],
  ): () => void {
    const types = type_filter == null
      ? null
      : Array.isArray(type_filter) ? new Set(type_filter) : new Set([type_filter])
    const sources = source_filter == null
      ? null
      : Array.isArray(source_filter) ? new Set(source_filter) : new Set([source_filter])

    const handler = () => {
      const latest = this.events[this.events.length - 1]
      if (latest == null) {
        return
      }
      if (types != null && !types.has(latest.type)) {
        return
      }
      if (sources != null && !sources.has(latest.source)) {
        return
      }
      callback(latest)
    }

    this.on_change(this.properties.events, handler)

    return () => {
      this.remove_on_change(this.properties.events, handler)
    }
  }

  clear(type?: InteractionEventType, source?: string): void {
    let filtered = this.events
    if (type != null) {
      filtered = filtered.filter((e) => e.type !== type)
    }
    if (source != null) {
      filtered = filtered.filter((e) => e.source !== source)
    }
    this.events = filtered
    this._update_filtered_views(filtered)
  }

  clear_by_source(source: string, type?: InteractionEventType): void {
    this.clear(type, source)
  }

  clear_all(): void {
    this.events = []
    this._update_filtered_views([])
  }

  get_events(
    type?: InteractionEventType,
    source?: string,
  ): InteractionEventPayload[] {
    return this.events.filter((e) => {
      if (type != null && e.type !== type) {
        return false
      }
      if (source != null && e.source !== source) {
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
      switch (e.type) {
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
