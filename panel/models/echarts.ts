import {ModelEvent} from "@bokehjs/core/bokeh_events"
import {div} from "@bokehjs/core/dom"
import type * as p from "@bokehjs/core/properties"
import type {Attrs} from "@bokehjs/core/types"
import {Ref} from "@bokehjs/core/kinds"

import {serializeEvent} from "./event-to-object"
import {HTMLBox, HTMLBoxView} from "./layout"
import {InteractionStore, type InteractionEventKind, type InteractionSelection, type InteractionViewport} from "./interaction_store"
import {transformJsPlaceholders} from "./util"

const mouse_events = [
  "click", "dblclick", "mousedown", "mousemove", "mouseup", "mouseover", "mouseout",
  "globalout", "contextmenu",
]

const events = [
  "highlight", "downplay", "selectchanged", "legendselectchangedEvent", "legendselected",
  "legendunselected", "legendselectall", "legendinverseselect", "legendscroll", "datazoom",
  "datarangeselected", "timelineplaychanged", "restore", "dataviewchanged", "magictypechanged",
  "geoselectchanged", "geoselected", "geounselected", "axisareaselected", "brush", "brushEnd",
  "rushselected", "globalcursortaken", "rendered", "finished",
]

const all_events = mouse_events.concat(events)

export class EChartsEvent extends ModelEvent {
  constructor(readonly type: string, readonly data: any, readonly query: string) {
    super()
  }

  protected override get event_values(): Attrs {
    return {model: this.origin, type: this.type, data: this.data, query: this.query}
  }

  static {
    this.prototype.event_name = "echarts_event"
  }
}

export class EChartsView extends HTMLBoxView {
  declare model: ECharts

  container: HTMLDivElement
  _chart: any
  _callbacks: Array<any>[] = []
  _loading_interval: ReturnType<typeof setInterval> | null = null
  _loading_timeout: ReturnType<typeof setTimeout> | null = null
  _loading_el: HTMLDivElement | null = null

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
      source: "echarts",
      source_id: this.model.id,
      payload,
      selection: selection ?? undefined,
      viewport: viewport ?? undefined,
    })
  }

  _classify_echarts_event(name: string): InteractionEventKind {
    const lower = name.toLowerCase()
    if (lower.includes("mouseover") || lower.includes("mouseenter")) {
      return "hover"
    }
    if (lower.includes("zoom") || lower.includes("roam") || lower.includes("timelinechanged")) {
      return "viewport"
    }
    return "selection"
  }

  _normalize_selection(event_type: string, event: any): InteractionSelection | null {
    const indices: number[] = []
    const values: Array<{[field: string]: any}> = []
    const ranges: {[axis: string]: [any, any]} = {}
    if (event == null) {
      return null
    }
    if (typeof event.dataIndex === "number") {
      indices.push(event.dataIndex)
    }
    if (event.data != null && typeof event.data === "object") {
      values.push({...event.data})
    }
    if (event.name != null) {
      values.push({name: event.name})
    }
    if (event.batch != null && Array.isArray(event.batch)) {
      for (const b of event.batch) {
        if (b.selected != null) {
          for (const item of b.selected) {
            if (typeof item.dataIndex === "number") indices.push(item.dataIndex)
            if (item.data != null) values.push({...item.data})
          }
        }
      }
    }
    if (event.fromName != null || event.toName != null) {
      if (Array.isArray(event.range)) {
        ranges[event.fromName || "x"] = event.range
      }
    }
    const lower = event_type.toLowerCase()
    const is_range = (
      lower.includes("brush") ||
      lower.includes("axisareaselected") ||
      (event.batch != null && event.batch.some((b: any) => b.brushType != null))
    )
    if (is_range && Object.keys(ranges).length === 0) {
      if (event.areas != null && Array.isArray(event.areas)) {
        for (const area of event.areas) {
          if (area.coordRange != null) {
            ranges[`area_${Object.keys(ranges).length}`] = area.coordRange
          }
        }
      }
    }
    if (is_range || Object.keys(ranges).length > 0) {
      return {mode: "range", ranges, indices, values}
    }
    if (indices.length === 0 && values.length === 0) {
      return null
    }
    return {mode: "point", indices, values}
  }

  _normalize_viewport(event_type: string, event: any): InteractionViewport | null {
    const ranges: {[axis: string]: [any, any]} = {}
    if (event == null) {
      return null
    }
    if (Array.isArray(event.start) && Array.isArray(event.end)) {
      ranges["x"] = [event.start[0], event.end[0]]
      ranges["y"] = [event.start[1], event.end[1]]
    }
    if (event.batch != null && Array.isArray(event.batch)) {
      for (const b of event.batch) {
        if (b.startValue != null && b.endValue != null) {
          ranges[b.zoomId || b.dataZoomId || "x"] = [b.startValue, b.endValue]
        }
      }
    }
    if (Object.keys(ranges).length === 0) {
      return null
    }
    return {ranges}
  }

  override connect_signals(): void {
    super.connect_signals()
    const {width, height, renderer, theme, event_config, js_events, data} = this.model.properties
    this.on_change(data, () => this._plot())
    this.on_change([width, height], () => this._resize())
    this.on_change([theme, renderer], () => {
      this.render()
      if (this._chart != null) {
        this._chart.resize()
      }
    })
    this.on_change([event_config, js_events], () => this._subscribe())
  }

  override render(): void {
    if (this._chart != null) {
      try {
        (window as any).echarts.dispose(this._chart)
      } catch (e) {
        // dispose may fail if echarts was never fully initialized
      }
    }
    this._clear_loading_timer()
    super.render()
    this.container = div({style: {height: "100%", width: "100%"}})
    this.shadow_el.append(this.container)

    if ((window as any).echarts == null) {
      this._show_loading()
      this._await_echarts()
      return
    }
    this._init_chart()
  }

  _show_loading(): void {
    this._loading_el = div({
      style: {
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        height: "100%",
        width: "100%",
        color: "#888",
        fontSize: "14px",
      },
    })
    this._loading_el.textContent = "Loading ECharts..."
    this.container.append(this._loading_el)
  }

  _hide_loading(): void {
    if (this._loading_el != null) {
      this._loading_el.remove()
      this._loading_el = null
    }
  }

  _await_echarts(): void {
    // Try script onload listener first (event-driven, not polling)
    const script = document.querySelector("script[src*='echarts']")
    if (script != null) {
      const onLoad = () => {
        script.removeEventListener("load", onLoad)
        this._clear_loading_timer()
        this._hide_loading()
        this._init_chart()
      }
      script.addEventListener("load", onLoad)
    }

    // Polling fallback in case script tag isn't found or onload doesn't fire
    this._loading_interval = setInterval(() => {
      if ((window as any).echarts != null) {
        this._clear_loading_timer()
        this._hide_loading()
        this._init_chart()
      }
    }, 50)
    this._loading_timeout = setTimeout(() => {
      this._clear_loading_timer()
      this._hide_loading()
      console.warn(
        "ECharts library failed to load. Ensure you call pn.extension('echarts') " +
        "before using Gauge or ECharts components.",
      )
    }, 10000)
  }

  _clear_loading_timer(): void {
    if (this._loading_interval != null) {
      clearInterval(this._loading_interval)
      this._loading_interval = null
    }
    if (this._loading_timeout != null) {
      clearTimeout(this._loading_timeout)
      this._loading_timeout = null
    }
  }

  _init_chart(): void {
    if ((window as any).echarts == null) {
      return
    }
    this._hide_loading()
    const config = {width: this.model.width, height: this.model.height, renderer: this.model.renderer}
    this._chart = (window as any).echarts.init(
      this.container,
      this.model.theme,
      config,
    )
    this._plot()
    this._subscribe()
  }

  override remove(): void {
    this._clear_loading_timer()
    super.remove()
    if (this._chart != null) {
      try {
        (window as any).echarts.dispose(this._chart)
      } catch (e) {
        // dispose may fail if echarts was never fully initialized
      }
    }
  }

  override after_layout(): void {
    super.after_layout()
    if (this._chart != null) {
      this._chart.resize()
    }
  }

  _plot(): void {
    if ((window as any).echarts == null || this._chart == null) {
      return
    }
    const data = transformJsPlaceholders(this.model.data)
    this._chart.setOption(data, this.model.options)
  }

  _resize(): void {
    if (this._chart != null) {
      this._chart.resize({width: this.model.width, height: this.model.height})
    }
  }

  _subscribe(): void {
    if ((window as any).echarts == null || this._chart == null) {
      return
    }
    for (const [event_type, callback] of this._callbacks) {
      this._chart.off(event_type, callback)
    }
    this._callbacks = []
    for (const event_type in this.model.event_config) {
      if (!all_events.includes(event_type)) {
        console.warn(`Could not subscribe to unknown Echarts event: ${event_type}.`)
        continue
      }
      const queries = this.model.event_config[event_type]
      for (const query of queries) {
        const callback = (event: any) => {
          const processed = {...event}
          processed.event = serializeEvent(event.event?.event)
          const serialized = JSON.parse(JSON.stringify(processed))
          this.model.trigger_event(new EChartsEvent(event_type, serialized, query))
          const kind = this._classify_echarts_event(event_type)
          const payload = {event: event_type, query, data: serialized}
          if (kind === "viewport") {
            const vp = this._normalize_viewport(event_type, serialized)
            this._publish(kind, null, vp, payload)
          } else {
            const sel = this._normalize_selection(event_type, serialized)
            this._publish(kind, sel, null, payload)
          }
        }
        if (query != null) {
          this._chart.on(event_type, query, callback)
        } else {
          this._chart.on(event_type, callback)
        }
        this._callbacks.push([event_type, callback])
      }
    }
    for (const event_type in this.model.js_events) {
      if (!all_events.includes(event_type)) {
        console.warn(`Could not subscribe to unknown Echarts event: ${event_type}.`)
        continue
      }
      const handlers = this.model.js_events[event_type]
      for (const handler of handlers) {
        const callback = (event: any) => {
          handler.callback.execute(this._chart, event)
        }
        if ("query" in handler) {
          this._chart.on(event_type, handler.query, callback)
        } else {
          this._chart.on(event_type, callback)
        }
        this._callbacks.push([event_type, callback])
      }
    }
  }
}

export namespace ECharts {
  export type Attrs = p.AttrsOf<Props>
  export type Props = HTMLBox.Props & {
    data: p.Property<any>
    options: p.Property<any>
    event_config: p.Property<any>
    js_events: p.Property<any>
    renderer: p.Property<string>
    theme: p.Property<string>
    interaction_store: p.Property<InteractionStore | null>
  }
}

export interface ECharts extends ECharts.Attrs {}

export class ECharts extends HTMLBox {
  declare properties: ECharts.Props

  constructor(attrs?: Partial<ECharts.Attrs>) {
    super(attrs)
  }

  static override __module__ = "panel.models.echarts"

  static {
    this.prototype.default_view = EChartsView

    this.define<ECharts.Props>(({Any, Str, Nullable, Ref}) => ({
      data:          [ Any,           {} ],
      options:       [ Any,           {} ],
      event_config:  [ Any,           {} ],
      js_events:     [ Any,           {} ],
      theme:         [ Str,  "default"],
      renderer:      [ Str,   "canvas"],
      interaction_store: [ Nullable(Ref(InteractionStore)), null ],
    }))
  }
}
