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
    resources: p.Property<{[key: string]: {[key: string]: string}}>
    extension_themes: p.Property<{[key: string]: any}>
    fast_style: p.Property<{[key: string]: any}>
    bs_theme: p.Property<string>
  }
}

export interface PanelThemeManager extends PanelThemeManager.Attrs {}

export class PanelThemeManagerView extends View {
  declare model: PanelThemeManager

  _fast_design_provider: any = null

  override connect_signals(): void {
    super.connect_signals()
    const {design, theme, theme_name, base_css, theme_css, css_variables, is_dark, bokeh_theme_json, resources, extension_themes, fast_style, bs_theme} = this.model.properties
    this.on_change([design, theme, theme_name, base_css, theme_css, css_variables, is_dark, bokeh_theme_json, resources, extension_themes, fast_style, bs_theme], () => {
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
    this._apply_resources()
    this._apply_design_classes()
    this._apply_fast_provider()
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

  _apply_resources(): void {
    const resources = this.model.resources || {}
    this._apply_css_resources(resources['css'] || {})
    this._apply_font_resources(resources['font'] || {})
  }

  _apply_css_resources(css_map: {[key: string]: string}): void {
    const existing = document.querySelectorAll('link[data-panel-design-css]')
    const toRemove: Element[] = []
    const neededIds = new Set<string>()
    for (const name in css_map) {
      neededIds.add(`panel-design-css-${name}`)
    }
    existing.forEach(el => {
      if (!neededIds.has(el.getAttribute('data-panel-design-css') || '')) {
        toRemove.push(el)
      }
    })
    toRemove.forEach(el => el.remove())

    for (const name in css_map) {
      const url = css_map[name]
      const id = `panel-design-css-${name}`
      if (document.getElementById(id)) continue
      const link = document.createElement('link')
      link.id = id
      link.rel = 'stylesheet'
      link.type = 'text/css'
      link.href = url
      link.setAttribute('data-panel-design-css', name)
      document.head.appendChild(link)
    }
  }

  _apply_font_resources(font_map: {[key: string]: string}): void {
    const existing = document.querySelectorAll('link[data-panel-design-font]')
    const toRemove: Element[] = []
    const neededIds = new Set<string>()
    for (const name in font_map) {
      neededIds.add(`panel-design-font-${name}`)
    }
    existing.forEach(el => {
      if (!neededIds.has(el.getAttribute('data-panel-design-font') || '')) {
        toRemove.push(el)
      }
    })
    toRemove.forEach(el => el.remove())

    for (const name in font_map) {
      const url = font_map[name]
      const id = `panel-design-font-${name}`
      if (document.getElementById(id)) continue
      const link = document.createElement('link')
      link.id = id
      link.rel = 'stylesheet'
      link.type = 'text/css'
      link.href = url
      link.setAttribute('data-panel-design-font', name)
      document.head.appendChild(link)
    }
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
      const bsTheme = this.model.bs_theme || (this.model.is_dark ? 'dark' : 'light')
      html.setAttribute('data-bs-theme', bsTheme)
      html.setAttribute('data-theme', this.model.is_dark ? 'dark' : 'light')
      html.setAttribute('data-panel-theme', this.model.theme)
      html.setAttribute('data-panel-design', this.model.design)
    }
  }

  _apply_fast_provider(): void {
    if (this.model.design !== 'fast') {
      this._fast_design_provider = null
      return
    }
    const fastStyle = this.model.fast_style || {}
    if (Object.keys(fastStyle).length === 0) return
    if (typeof (window as any).fastDesignProvider === 'undefined') return

    if (!this._fast_design_provider) {
      try {
        this._fast_design_provider = new (window as any).fastDesignProvider(document.body)
      } catch (e) {
        return
      }
    }
    const dp = this._fast_design_provider
    if (!dp) return

    if (fastStyle.accent_base_color && typeof dp.setAccentColor === 'function') {
      dp.setAccentColor(fastStyle.accent_base_color)
    }
    if (fastStyle.neutral_color && typeof dp.setNeutralColor === 'function') {
      dp.setNeutralColor(fastStyle.neutral_color)
    }
    if (fastStyle.background_color && typeof dp.setBackgroundColor === 'function') {
      dp.setBackgroundColor(fastStyle.background_color)
    }
    if (fastStyle.luminance !== undefined && typeof dp.setLuminance === 'function') {
      dp.setLuminance(fastStyle.luminance)
    }
    if (fastStyle.corner_radius !== undefined && typeof dp.setCornerRadius === 'function') {
      dp.setCornerRadius(fastStyle.corner_radius)
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
        resources: {...this.model.resources},
        extension_themes: {...this.model.extension_themes},
        fast_style: {...this.model.fast_style},
        bs_theme: this.model.bs_theme,
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
      resources:          [ Dict(Dict(Str)), {} ],
      extension_themes:   [ Dict(Str), {} ],
      fast_style:         [ Dict(Str), {} ],
      bs_theme:           [ Str,      'light' ],
    }))
  }
}
