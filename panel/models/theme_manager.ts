import type * as p from "@bokehjs/core/properties"
import {View} from "@bokehjs/core/view"
import {Model} from "@bokehjs/model"

export namespace PanelThemeManager {
  export type Attrs = p.AttrsOf<Props>
  export type Props = Model.Props & {
    design: p.Property<string>
    theme: p.Property<string>
    theme_name: p.Property<string>
    base_css: p.Property<string>
    theme_css: p.Property<string>
    css_variables: p.Property<{[key: string]: any}>
    design_name: p.Property<string>
    is_dark: p.Property<boolean>
    bokeh_theme_json: p.Property<{[key: string]: any}>
  }
}

export interface PanelThemeManager extends PanelThemeManager.Attrs {}

export class PanelThemeManagerView extends View {
  declare model: PanelThemeManager

  override connect_signals(): void {
    super.connect_signals()
    const {design, theme, theme_name, base_css, theme_css, css_variables, is_dark, bokeh_theme_json} = this.model.properties
    this.on_change([design, theme, theme_name, base_css, theme_css, css_variables, is_dark, bokeh_theme_json], () => {
      this._apply_theme()
    })
  }

  override render(): void {
    super.render()
    this._apply_theme()
  }

  _apply_theme(): void {
    this._apply_css_variables()
    this._apply_base_css()
    this._apply_theme_css()
    this._apply_design_classes()
    this._dispatch_theme_event()
  }

  _apply_css_variables(): void {
    const root = document.documentElement
    if (!root) return
    const variables = this.model.css_variables
    for (const key in variables) {
      const value = variables[key]
      if (value != null) {
        root.style.setProperty(key, String(value))
      }
    }
  }

  _apply_base_css(): void {
    if (!this.model.base_css) return
    this._inject_style('panel-theme-base', this.model.base_css)
  }

  _apply_theme_css(): void {
    if (!this.model.theme_css) return
    this._inject_style('panel-theme-specific', this.model.theme_css)
  }

  _inject_style(id: string, css: string): void {
    let styleEl = document.getElementById(id) as HTMLStyleElement | null
    if (!styleEl) {
      styleEl = document.createElement('style')
      styleEl.id = id
      document.head.appendChild(styleEl)
    }
    styleEl.textContent = css
  }

  _apply_design_classes(): void {
    const body = document.body
    if (!body) return
    body.classList.remove('panel-design-fast', 'panel-design-bootstrap', 'panel-design-material', 'panel-design-native')
    body.classList.remove('theme-dark', 'theme-default', 'theme-light')
    body.classList.add(`panel-design-${this.model.design}`)
    body.classList.add(this.model.is_dark ? 'theme-dark' : 'theme-default')
    const html = document.documentElement
    if (html) {
      html.setAttribute('data-bs-theme', this.model.is_dark ? 'dark' : 'light')
      html.setAttribute('data-theme', this.model.is_dark ? 'dark' : 'light')
      html.setAttribute('data-panel-theme', this.model.theme)
      html.setAttribute('data-panel-design', this.model.design)
    }
  }

  _dispatch_theme_event(): void {
    const event = new CustomEvent('panel:themechange', {
      detail: {
        design: this.model.design,
        theme: this.model.theme,
        theme_name: this.model.theme_name,
        is_dark: this.model.is_dark,
        css_variables: {...this.model.css_variables},
        bokeh_theme_json: {...this.model.bokeh_theme_json},
      }
    })
    document.dispatchEvent(event)
    window.dispatchEvent(event)
  }
}

export class PanelThemeManager extends Model {
  declare properties: PanelThemeManager.Props

  static override __module__ = "panel.models.theme_manager"

  static {
    this.prototype.default_view = PanelThemeManagerView
    this.define<PanelThemeManager.Props>(({Bool, Dict, Str}) => ({
      design:             [ Str,      'native' ],
      theme:              [ Str,      'default' ],
      theme_name:         [ Str,      '' ],
      base_css:           [ Str,      '' ],
      theme_css:          [ Str,      '' ],
      css_variables:      [ Dict(Str), {} ],
      design_name:        [ Str,      '' ],
      is_dark:            [ Bool,     false ],
      bokeh_theme_json:   [ Dict(Str), {} ],
    }))
  }
}
