import {div} from "@bokehjs/core/dom"
import type * as p from "@bokehjs/core/properties"
import {ModelEvent} from "@bokehjs/core/bokeh_events"
import {isArray} from "@bokehjs/core/util/types"
import {LayoutDOM, LayoutDOMView} from "@bokehjs/models/layouts/layout_dom"
import type {Attrs} from "@bokehjs/core/types"

import {set_size} from "./layout"

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
  _last_object_version: number = -1
  _last_structural_signature: string | null = null
  _last_theme: any = null
  _last_show_actions: any = null

  override connect_signals(): void {
    super.connect_signals()
    const {data, show_actions, theme, data_sources, events} = this.model.properties
    this._replot = debounce(() => this._plot(), 20)
    this.on_change([data, show_actions, theme], () => {
      this._replot()
    })
    this.on_change(data_sources, () => this._connect_sources())
    this.on_change(events, () => {
      if (this.vega_view == null) {
        return
      }
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
        if (index._vgsid_ !== undefined) {  // If "_vgsid_" property exists
          indexes.push(index._vgsid_)
        } else {  // If "_vgsid_" property doesn't exist
          // Iterate through all properties in the "index" object
          for (const key in index) {
            if (index.hasOwnProperty(key)) {  // To ensure key comes from "index" object itself, not its prototype
              indexes.push({[key]: index[key]})  // Push a new object with this key-value pair into the array
            }
          }
        }
      }
      value = indexes
    }
    this.model.trigger_event(new VegaEvent({type: name, value}))
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

  _is_data_row_array(arr: any[]): boolean {
    if (arr.length === 0) return false
    const sample = arr[0]
    if (sample == null || typeof sample !== "object") return false
    if (Array.isArray(sample)) return false
    const structural_keys = ["mark", "encoding", "layer", "concat", "vconcat",
      "hconcat", "facet", "repeat", "transform", "signal", "param",
      "resolve", "projection", "selection", "data", "scale", "axis",
      "legend", "title", "config", "name", "type", "expr", "init"]
    const keys = Object.keys(sample)
    return keys.length > 0 && !keys.some(k => structural_keys.indexOf(k) >= 0)
  }

  _strip_structural_spec(obj: any): any {
    if (obj == null) return obj
    if (Array.isArray(obj)) {
      if (this._is_data_row_array(obj)) return "__DATA_ROWS__"
      return obj.map(item => this._strip_structural_spec(item))
    }
    if (typeof obj === "object") {
      const result: any = {}
      for (const key of Object.keys(obj)) {
        if (key === "values") {
          const val = obj[key]
          if (typeof val === "string") {
            result[key] = val
          } else if (Array.isArray(val)) {
            result[key] = "__VALUES__"
          } else {
            result[key] = this._strip_structural_spec(val)
          }
        } else if (key === "datasets") {
          const val = obj[key] || {}
          result[key] = Object.keys(val).sort()
        } else {
          result[key] = this._strip_structural_spec(obj[key])
        }
      }
      return result
    }
    return obj
  }

  _structural_signature(data: any, theme: any, show_actions: any): string {
    const stripped = this._strip_structural_spec(JSON.parse(JSON.stringify(data)))
    return JSON.stringify({spec: stripped, theme, show_actions})
  }

  _plot(): void {
    const data = this.model.data
    if ((data == null) || !(window as any).vegaEmbed) {
      return
    }
    const object_changed = this.model._object_version !== this._last_object_version

    const current_signature = this._structural_signature(data, this.model.theme, this.model.show_actions)
    const structural_changed = object_changed || current_signature !== this._last_structural_signature
    this._last_structural_signature = current_signature
    this._last_theme = this.model.theme
    this._last_show_actions = this.model.show_actions

    if (object_changed) {
      if (this.vega_view != null) {
        this.vega_view.finalize()
        this.vega_view = null
      }
      this._callbacks = []
      this.container.innerHTML = ""
      this._last_object_version = this.model._object_version
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

      if (!structural_changed && this.vega_view != null) {
        this._update_view_data(datasets)
        return
      }
    }

    if (!structural_changed && this.vega_view != null) {
      return
    }

    if (this.vega_view != null) {
      this.vega_view.finalize()
      this.vega_view = null
      this._callbacks = []
      this.container.innerHTML = ""
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

  _update_view_data(datasets: any): void {
    for (const name in datasets) {
      try {
        this.vega_view.data(name, datasets[name])
      } catch (e) {
        console.warn(`Failed to update Vega dataset '${name}':`, e)
      }
    }
    this.vega_view.run()
    if (this._resize != null) {
      this._resize()
    }
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
    _object_version: p.Property<number>
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

    this.define<VegaPlot.Props>(({Any, List, Bool, Nullable, Str, Float}) => ({
      data:         [ Any,                {} ],
      data_sources: [ Any,                {} ],
      events:       [ List(Str),      [] ],
      show_actions: [ Bool,         false ],
      theme:        [ Nullable(Str), null ],
      throttle:     [ Any,                {} ],
      _object_version: [ Float, 0 ],
    }))
  }
}
