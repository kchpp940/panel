import {div} from "@bokehjs/core/dom"
import type * as p from "@bokehjs/core/properties"
import {ModelEvent} from "@bokehjs/core/bokeh_events"
import {isArray} from "@bokehjs/core/util/types"
import {Ref} from "@bokehjs/core/kinds"
import {LayoutDOM, LayoutDOMView} from "@bokehjs/models/layouts/layout_dom"
import type {Attrs} from "@bokehjs/core/types"

import {set_size} from "./layout"
import {
  InteractionStore,
  type InteractionEventKind,
  type InteractionSelection,
  type InteractionViewport,
  build_point_filters,
  build_range_filters,
  collect_fields,
} from "./interaction_store"

import {debounce} from  "debounce"

export class VegaEvent extends ModelEvent {
  constructor(readonly data: any) {
    super()
  }

  protected override get event_values(): Attrs {
    return {model: this.origin, data: this.data}
  }

  static {
    this.prototype.event_name = "vega_event"
  }
}

export class VegaPlotView extends LayoutDOMView {
  declare model: VegaPlot

  vega_view: any
  container: HTMLDivElement
  _callbacks: string[]
  _connected: string[]
  _replot: any
  _resize: any
  _rendered: boolean = false

  _publish(
    kind: InteractionEventKind,
    selection: InteractionSelection | null,
    viewport: InteractionViewport | null,
    payload: any,
  ): void {
    if (this.model.interaction_store == null) {
      return
    }
    this.model.interaction_store.publish_event({
      kind,
      source: "vega",
      source_id: this.model.id,
      payload,
      selection: selection ?? undefined,
      viewport: viewport ?? undefined,
    })
  }

  _classify_vega_event(name: string, value: any): InteractionEventKind {
    const lower = name.toLowerCase()
    if (lower.includes("hover") || lower.includes("mouseover") || lower.includes("mouseenter")) {
      return "hover"
    }
    if (lower.includes("view") || lower.includes("zoom") || lower.includes("pan") || lower.includes("scale") || lower.includes("domain")) {
      return "viewport"
    }
    return "selection"
  }

  _get_dataset_id(): string | undefined {
    const ds = this.model.data_sources
    if (ds != null) {
      const keys = Object.keys(ds)
      if (keys.length > 0) return keys[0]
    }
    return undefined
  }

  _normalize_selection(name: string, value: any): InteractionSelection | null {
    if (value == null) {
      return null
    }
    const indices: number[] = []
    const values: Array<{[field: string]: any}> = []
    if (Array.isArray(value)) {
      for (let i = 0; i < value.length; i++) {
        const item = value[i]
        if (typeof item === "number") {
          indices.push(item)
        } else if (item && typeof item === "object") {
          if (typeof item._vgsid_ === "number") {
            indices.push(item._vgsid_)
          }
          values.push({...item})
        }
      }
    } else if (typeof value === "object") {
      if (Array.isArray(value.vlPoint?.or)) {
        for (const item of value.vlPoint.or) {
          if (typeof item._vgsid_ === "number") indices.push(item._vgsid_)
          values.push({...item})
        }
      }
      const ranges: {[axis: string]: [any, any]} = {}
      let is_range = false
      for (const k of Object.keys(value)) {
        const v = value[k]
        if (Array.isArray(v) && v.length === 2) {
          ranges[k] = v
          is_range = true
        }
      }
      if (is_range) {
        const dataset_id = this._get_dataset_id()
        const allowed_fields = this.model.interaction_fields
        const fields = collect_fields(values, Object.keys(ranges), allowed_fields)
        const filters = build_range_filters(ranges, allowed_fields)
        if (indices.length > 0) {
          filters.unshift({field: "index", op: "in", value: indices.slice()})
        }
        return {mode: "range", dataset_id, ranges, fields, indices, values, filters}
      }
      for (const k of Object.keys(value)) {
        const v = value[k]
        if (typeof v === "object" && v != null && Array.isArray((v as any).vlPoint?.or)) {
          for (const item of (v as any).vlPoint.or) {
            if (typeof item._vgsid_ === "number") indices.push(item._vgsid_)
            values.push({...item})
          }
        }
      }
    }
    if (indices.length === 0 && values.length === 0) {
      return null
    }
    const dataset_id = this._get_dataset_id()
    const allowed_fields = this.model.interaction_fields
    const fields = collect_fields(values, [], allowed_fields)
    const filters = build_point_filters(values, indices, allowed_fields)
    return {mode: "point", dataset_id, indices, fields, values, filters}
  }

  _normalize_viewport(name: string, value: any): InteractionViewport | null {
    const ranges: {[axis: string]: [any, any]} = {}
    if (value == null) {
      return null
    }
    if (typeof value === "object") {
      for (const k of Object.keys(value)) {
        const v = value[k]
        if (Array.isArray(v) && v.length === 2) {
          ranges[k] = v
        }
      }
    }
    if (Object.keys(ranges).length === 0) {
      return null
    }
    const dataset_id = this._get_dataset_id()
    const allowed_fields = this.model.interaction_fields
    const fields = Object.keys(ranges)
    const filters = build_range_filters(ranges, allowed_fields)
    return {dataset_id, fields, ranges, filters}
  }

  override connect_signals(): void {
    super.connect_signals()
    const {data, show_actions, theme, data_sources, events} = this.model.properties
    this._replot = debounce(() => this._plot(), 20)
    this.on_change([data, show_actions, theme], () => {
      this._replot()
    })
    this.on_change(data_sources, () => this._connect_sources())
    this.on_change(events, () => {
      for (const event of this.model.events) {
        if (this._callbacks.indexOf(event) > -1) {
          continue
        }
        this._callbacks.push(event)
        const callback = (name: string, value: any) => this._dispatch_event(name, value)
        const timeout = this.model.throttle[event] || 20
        this.vega_view.addSignalListener(event, debounce(callback, timeout, false))
      }
    })
    this._connected = []
    this._connect_sources()
  }

  _connect_sources(): void {
    for (const ds in this.model.data_sources) {
      const cds = this.model.data_sources[ds]
      if (this._connected.indexOf(ds) < 0) {
        this.connect(cds.properties.data.change, () => this._replot())
        this._connected.push(ds)
      }
    }
  }

  override remove(): void {
    this.vega_view?.finalize()
    super.remove()
  }

  _dispatch_event(name: string, value: any): void {
    if ("vlPoint" in value && value.vlPoint.or != null) {
      const indexes = []
      for (const index of value.vlPoint.or) {
        if (index._vgsid_ !== undefined) {
          indexes.push(index._vgsid_)
        } else {
          for (const key in index) {
            if (index.hasOwnProperty(key)) {
              indexes.push({[key]: index[key]})
            }
          }
        }
      }
      value = indexes
    }
    this.model.trigger_event(new VegaEvent({type: name, value}))
    const kind = this._classify_vega_event(name, value)
    const payload = {signal: name, value}
    if (kind === "viewport") {
      const vp = this._normalize_viewport(name, value)
      this._publish(kind, null, vp, payload)
    } else {
      const sel = this._normalize_selection(name, value)
      this._publish(kind, sel, null, payload)
    }
  }

  _fetch_datasets() {
    const datasets: any = {}
    for (const ds in this.model.data_sources) {
      const cds = this.model.data_sources[ds]
      const data: any = []
      const columns = cds.columns()
      for (let i = 0; i < cds.get_length(); i++) {
        const item: any = {}
        for (const column of columns) {
          item[column] = cds.data[column][i]
        }
        data.push(item)
      }
      datasets[ds] = data
    }
    return datasets
  }

  get child_models(): LayoutDOM[] {
    return []
  }

  override render(): void {
    super.render()
    this._rendered = false
    this.container = div()
    set_size(this.container, this.model)
    this._callbacks = []
    this._plot()
    this.shadow_el.append(this.container)
  }

  _plot(): void {
    const data = this.model.data
    if ((data == null) || !(window as any).vegaEmbed) {
      return
    }
    if (this.model.data_sources && (Object.keys(this.model.data_sources).length > 0)) {
      const datasets = this._fetch_datasets()
      if ("data" in datasets) {
        data.data.values = datasets.data
        delete datasets.data
      }
      if (data.data != null) {
        const data_objs = isArray(data.data) ? data.data : [data.data]
        for (const d of data_objs) {
          if (d.name in datasets) {
            d.values = datasets[d.name]
            delete datasets[d.name]
          }
        }
      }
      this.model.data.datasets = datasets
    }
    const config: any = {actions: this.model.show_actions, theme: this.model.theme};

    (window as any).vegaEmbed(this.container, this.model.data, config).then((result: any) => {
      this.vega_view = result.view
      this._resize = debounce(() => this.resize_view(result.view), 50)
      const callback = (name: string, value: any) => this._dispatch_event(name, value)
      for (const event of this.model.events) {
        this._callbacks.push(event)
        const timeout = this.model.throttle[event] || 20
        this.vega_view.addSignalListener(event, debounce(callback, timeout, false))
      }
    })
  }

  override after_layout(): void {
    super.after_layout()
    if (this.vega_view != null) {
      this._resize()
    }
  }

  resize_view(view: any): void {
    const canvas = view._renderer.canvas()
    if (!this._rendered && canvas !== null) {
      for (const listener of view._eventListeners) {
        if (listener.type === "resize") {
          listener.handler(new Event("resize"))
        }
      }
      this._rendered = true
    }
  }
}

export namespace VegaPlot {
  export type Attrs = p.AttrsOf<Props>
  export type Props = LayoutDOM.Props & {
    data: p.Property<any>
    data_sources: p.Property<any>
    events: p.Property<string[]>
    show_actions: p.Property<boolean>
    theme: p.Property<string | null>
    throttle: p.Property<any>
    interaction_store: p.Property<InteractionStore | null>
    interaction_fields: p.Property<string[] | null>
  }
}

export interface VegaPlot extends VegaPlot.Attrs {}

export class VegaPlot extends LayoutDOM {
  declare properties: VegaPlot.Props

  constructor(attrs?: Partial<VegaPlot.Attrs>) {
    super(attrs)
  }

  static override __module__ = "panel.models.vega"

  static {
    this.prototype.default_view = VegaPlotView

    this.define<VegaPlot.Props>(({Any, List, Bool, Nullable, Str, Ref}) => ({
      data:         [ Any,                {} ],
      data_sources: [ Any,                {} ],
      events:       [ List(Str),          [] ],
      show_actions: [ Bool,            false ],
      theme:        [ Nullable(Str),   null ],
      throttle:     [ Any,                {} ],
      interaction_store: [ Nullable(Ref(InteractionStore)), null ],
      interaction_fields: [ Nullable(List(Str)), null ],
    }))
  }
}
