import type {StyleSheetLike} from "@bokehjs/core/dom"
import type * as p from "@bokehjs/core/properties"

import {ReactiveHTML, ReactiveHTMLView} from "./reactive_html"

import gridstack_css from "styles/models/gridstack.css"

declare const GridStack: any

interface GridStackNode {
  el: HTMLElement
  x: number
  y: number
  w: number
  h: number
}

interface GridStackEngine {
  nodes: GridStackNode[]
  _notify(): void
}

interface GridStackInstance {
  engine: GridStackEngine
  opts: {row?: number}
  column(n: number): void
  cellHeight(value: number | "auto"): void
  enableMove(enabled: boolean): void
  enableResize(enabled: boolean): void
  on(event: string, callback: (event: Event, el?: HTMLElement) => void): void
  destroy(): void
}

export class GridStackView extends ReactiveHTMLView {
  declare model: GridStack

  protected _gridstack: GridStackInstance | null = null
  protected _resize_observer: ResizeObserver | null = null

  override connect_signals(): void {
    super.connect_signals()

    const {allow_drag, allow_resize, ncols, nrows} = this.model.data.properties
    this.on_change(allow_drag, () => this._update_allow_drag())
    this.on_change(allow_resize, () => this._update_allow_resize())
    this.on_change(ncols, () => this._update_ncols())
    this.on_change(nrows, () => this._update_nrows())
  }

  override stylesheets(): StyleSheetLike[] {
    return [...super.stylesheets(), gridstack_css]
  }

  protected _get_grid_el(): HTMLElement | null {
    const {id} = this.model.data
    const el = this.shadow_el.getElementById(`grid-${id}`)
    if (el != null) {
      return el
    }
    return document.getElementById(`grid-${id}`)
  }

  protected _get_height(): number {
    const grid = this._get_grid_el()
    return (this.model.data as any).height || (grid?.offsetHeight ?? 0) || (this.model as any).min_height || 0
  }

  protected _update_cell_height(): void {
    if (this._gridstack == null) {
      return
    }
    const data = this.model.data as any
    if (data.nrows) {
      const height = this._get_height()
      if (height > 0) {
        this._gridstack.cellHeight(Math.floor(height / data.nrows))
      }
    }
  }

  protected _sync_state(source: "user" | "layout"): void {
    if (this._gridstack == null) {
      return
    }
    const items: Array<{id: string | null; x0: number; y0: number; x1: number; y1: number}> = []
    for (const node of this._gridstack.engine.nodes) {
      const el = node.el
      el.setAttribute("gs-x", String(node.x))
      el.setAttribute("gs-y", String(node.y))
      el.setAttribute("gs-w", String(node.w))
      el.setAttribute("gs-h", String(node.h))
      items.push({
        id: el.getAttribute("data-id"),
        x0: node.x,
        y0: node.y,
        x1: node.x + node.w,
        y1: node.y + node.h,
      })
    }
    const data = this.model.data as any
    data._state_event = source
    data.state = items
  }

  protected _update_allow_drag(): void {
    if (this._gridstack != null) {
      this._gridstack.enableMove((this.model.data as any).allow_drag)
    }
  }

  protected _update_allow_resize(): void {
    if (this._gridstack != null) {
      this._gridstack.enableResize((this.model.data as any).allow_resize)
    }
  }

  protected _update_ncols(): void {
    if (this._gridstack != null) {
      this._gridstack.column((this.model.data as any).ncols)
      this._sync_state("layout")
    }
  }

  protected _update_nrows(): void {
    if (this._gridstack != null) {
      this._gridstack.opts.row = (this.model.data as any).nrows
      this._update_cell_height()
      this._sync_state("layout")
    }
  }

  override render(): void {
    super.render()

    const grid = this._get_grid_el()
    if (grid == null) {
      console.warn("GridStack container element 'grid' could not be found.")
      return
    }

    const data = this.model.data as any
    const options: any = {
      column: data.ncols,
      disableResize: !data.allow_resize,
      disableDrag: !data.allow_drag,
      margin: 0,
    }
    if (data.nrows) {
      options.row = data.nrows
      const height = this._get_height()
      if (height > 0) {
        options.cellHeight = Math.floor(height / data.nrows)
      }
    }

    this._gridstack = GridStack.init(options, grid)

    this._gridstack.on("resizestop", () => {
      this._sync_state("user")
      this.invalidate_layout()
    })
    this._gridstack.on("dragstop", () => {
      this._sync_state("user")
    })
    this._gridstack.on("change", () => {
      this._sync_state("layout")
    })

    this._resize_observer = new ResizeObserver(() => {
      this._update_cell_height()
    })
    this._resize_observer.observe(grid)

    this._sync_state("layout")
  }

  override _after_layout(): void {
    super._after_layout()
    this._update_nrows()
    this._update_cell_height()
    if (this._gridstack != null) {
      this._gridstack.engine._notify()
    }
  }

  override remove(): void {
    if (this._resize_observer != null) {
      this._resize_observer.disconnect()
      this._resize_observer = null
    }
    if (this._gridstack != null) {
      this._gridstack.destroy()
      this._gridstack = null
    }
    super.remove()
  }
}

export namespace GridStack {
  export type Attrs = p.AttrsOf<Props>
  export type Props = ReactiveHTML.Props
}

export interface GridStack extends GridStack.Attrs {}

export class GridStack extends ReactiveHTML {
  declare properties: GridStack.Props

  constructor(attrs?: Partial<GridStack.Attrs>) {
    super(attrs)
  }

  static override __module__ = "panel.models.gridstack"

  static {
    this.prototype.default_view = GridStackView
  }
}
