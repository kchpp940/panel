# Launching a server dynamically

The CLI `panel serve` command described below is usually the best approach for deploying applications. However when working on the REPL or embedding a Panel/Bokeh server in another application it is sometimes useful to dynamically launch a server, either using the `.show` method or using the `pn.serve` function.

## Previewing an application

Working from the command line will not automatically display rich representations inline as in a notebook, but you can still interact with your Panel components if you start a Bokeh server instance and open a separate browser window using the ``show`` method. The method has the following arguments:

``` console
title : str | None
  A string title to give the Document (if served as an app)
port: int (optional, default=0)
  Allows specifying a specific port
address : str
  The address the server should listen on for HTTP requests.
websocket_origin: str or list(str) (optional)
  A list of hosts that can connect to the websocket.
  This is typically required when embedding a server app in
  an external web site.
  If None, "localhost" is used.
threaded: boolean (optional, default=False)
  Whether to launch the Server on a separate thread, allowing
  interactive use.
verbose: boolean (optional, default=True)
  Whether to print the address and port
open : boolean (optional, default=True)
  Whether to open the server in a new browser tab
location : boolean or panel.io.location.Location
  Whether to create a Location component to observe and
  set the URL location.
```

To work with an app completely interactively you can set ``threaded=True`` which will launch the server on a separate thread and let you interactively play with the app.

<img src='https://assets.holoviews.org/panel/gifs/commandline_show.gif'></img>

The ``.show`` call will return either a Bokeh server instance (if ``threaded=False``) or a ``StoppableThread`` instance (if ``threaded=True``) which both provide a ``stop`` method to stop the server instance.

The ``pn.serve`` accepts a number of arguments:

``` console
    panel: Viewable, function or {str: Viewable or function}
      A Panel object, a function returning a Panel object or a
      dictionary mapping from the URL slug to either.
    port: int (optional, default=0)
      Allows specifying a specific port
    address : str
      The address the server should listen on for HTTP requests.
    websocket_origin: str or list(str) (optional)
      A list of hosts that can connect to the websocket.

      This is typically required when embedding a server app in
      an external web site.

      If None, "localhost" is used.
    loop : tornado.ioloop.IOLoop (optional, default=IOLoop.current())
      The tornado IOLoop to run the Server on
    show : boolean (optional, default=True)
      Whether to open the server in a new browser tab on start
    start : boolean(optional, default=True)
      Whether to start the Server
    title: str or {str: str} (optional, default=None)
      An HTML title for the application or a dictionary mapping
      from the URL slug to a customized title
    verbose: boolean (optional, default=True)
      Whether to print the address and port
    location : boolean or panel.io.location.Location
      Whether to create a Location component to observe and
      set the URL location.
    threaded: boolean (default=False)
      Whether to start the server on a new Thread
    admin: boolean (default=False)
      Whether to enable the admin panel
    run_diagnostics: bool (default=True)
      Whether to run startup diagnostics before starting the server.
    block_on_diagnostics_errors: bool (default=True)
      Whether to block server startup when diagnostics detect errors or fatal issues.
    admin_endpoint: str (default=None)
      Custom endpoint path for the admin panel.
    kwargs: dict
      Additional keyword arguments to pass to Server instance
```

## Startup Diagnostics

Panel provides a centralized startup diagnostics system built on a unified
`StartupConfig` that consolidates configuration from CLI arguments, programmatic
calls, and environment variables. This ensures consistent validation across all
server entry points (`CLI`, `pn.serve()`, and FastAPI integration).

The diagnostics cover:

- **WebSocket Origin**: Checks for wildcards, schemes, and ensures origins are properly configured.
- **Static Directories**: Validates that static directories exist, are readable, and don't conflict with reserved routes.
- **Autoreload**: Warns when autoreload is enabled (development-only feature) and checks file watcher availability.
- **Admin Endpoint**: Ensures the admin panel is protected by authentication and the endpoint is valid.
- **Session Cleanup**: Validates session history configuration and memory usage implications.
- **Notifications**: Checks notification module availability and configuration.
- **Browser Info**: Validates browser info module availability.

### Unified Startup Configuration

All server entry points use `StartupConfig.resolve()` to aggregate configuration
from multiple sources:

```python
from panel.io import StartupConfig, StartupMode

# Build a unified config from explicit values + environment variables + global config
cfg = StartupConfig.resolve(
    websocket_origin=["example.com"],
    admin=True,
    static_dirs={"/assets": "./assets"},
    session_history=100,
    # mode can be "auto", "development", or "production"
    mode=StartupMode.AUTO,
)

# Detect effective mode (development vs production)
print(cfg.detect_mode())  # StartupMode.DEVELOPMENT or StartupMode.PRODUCTION
```

### Development vs Production Mode

Diagnostics automatically detect whether the server is running in development or
production mode and adjust severity levels accordingly:

| Condition | Detected Mode |
|-----------|---------------|
| `--dev` / `--autoreload` flags | DEVELOPMENT |
| `PANEL_ENV=dev` or `PYTHON_ENV=dev` | DEVELOPMENT |
| Address bound to `localhost` / `127.0.0.1` | DEVELOPMENT |
| `PANEL_ENV=prod` or `PYTHON_ENV=prod` | PRODUCTION |
| Address bound to external interface | PRODUCTION |

In **DEVELOPMENT** mode, certain security-sensitive checks are downgraded from
FATAL/ERROR to WARNING/INFO so that routine local development workflows are not
unnecessarily blocked. In **PRODUCTION** mode, all checks run at full severity.

You can explicitly force the mode:

```python
from panel.io import StartupMode, validate_startup

# Force production-level checks even on localhost
result = validate_startup(
    websocket_origin=["*"],
    mode=StartupMode.PRODUCTION,
)
```

### Accessing Diagnostic Results

When diagnostics detect errors or fatal issues, the server startup is blocked by
default. You can access the diagnostic result programmatically:

```python
import panel as pn
from panel.io import state

# Create and configure your app
app = pn.Row("Hello, World!")

# Serve the app - diagnostics run automatically
server = pn.serve(app, start=False, port=5006)

# Access the last diagnostic result
if state._last_diagnostic_result:
    print(state._last_diagnostic_result.format_report())
    print("JSON output:", state._last_diagnostic_result.to_json())
    print("Startup mode:", state._last_diagnostic_result.startup_mode)
```

You can also run diagnostics independently:

```python
from panel.io import run_startup_diagnostics, StartupConfig

# Using StartupConfig.resolve() automatically reads env vars and config defaults
context = StartupConfig.resolve(
    websocket_origin=["example.com"],
    static_dirs={"/assets": "./assets"},
    admin=True,
    session_history=100,
)

result = run_startup_diagnostics(context)
print(result.format_report())
```

Or use the blocking validation wrapper:

```python
from panel.io import validate_startup

try:
    result = validate_startup(
        websocket_origin=["*"],  # This will trigger a FATAL error
        admin=True,
    )
except Exception as e:
    print(f"Startup blocked: {e}")
```

### Querying Effective Configuration and Server Status

After the server starts, you can query the actual effective configuration and
server status using the `state` object:

```python
import panel as pn
from panel.io import state

# Get the effective startup configuration as a dictionary
cfg = state.get_startup_config()
if cfg:
    print("Websocket origins:", cfg["websocket_origin"])
    print("Admin enabled:", cfg["admin"])
    print("Session history:", cfg["session_history"])
    print("Notifications:", cfg["notifications"])
    print("Browser info:", cfg["browser_info"])

# Get full structured server status (includes timestamp, config, diagnostics)
status = state.get_server_status()
print("Startup mode:", status["startup"]["startup_mode"])
print("Services:", list(status["startup"]["services"].keys()))

# Get status as formatted JSON string
json_status = state.get_server_status(as_json=True, indent=2)

# Print a human-readable status report to stdout
state.print_server_status(include_diagnostics=True)
```

The `get_server_status()` function returns a dictionary with:
- `timestamp`: ISO 8601 timestamp of when the status was captured
- `active_servers`: Count of currently running Panel servers
- `startup`: The effective startup configuration (same as `get_startup_config()` but includes per-service detail)
- `diagnostics`: The last diagnostic result (if `include_diagnostics=True`)

### Diagnostic Severity Levels

- **INFO**: Informational messages about configuration.
- **WARNING**: Potential issues that may affect functionality or security.
- **ERROR**: Configuration errors that may prevent features from working correctly.
- **FATAL**: Critical security or configuration issues that block server startup.

Note: In DEVELOPMENT mode, some ERROR/FATAL checks may be downgraded to
WARNING/INFO. The diagnostic report will indicate when running in DEVELOPMENT
mode.
