# State

The `pn.state` object makes various global state available and provides methods to manage that state.

## Parameters

`access_token`
: The access token issued by the OAuth provider to authorize requests to its APIs.

`busy`
: A boolean value to indicate whether a callback is being actively processed.

`cache`
: A global cache which can be used to share data between different processes.

`cookies`
: HTTP request cookies for the current session.

`curdoc`
: When running a server session this property holds the current bokeh Document.

`location`
: In a server context this provides read and write access to the URL:
   * `hash`: hash in window.location e.g. '#interact'
   * `pathname`: pathname in window.location e.g. '/user_guide/Interact.html'
   * `search`: search in window.location e.g. '?color=blue'
   * `reload`: Reloads the page when the location is updated.
   * `href` (readonly): The full url, e.g. 'https://localhost:80?color=blue#interact'
   * `hostname` (readonly): hostname in window.location e.g. 'panel.holoviz.org'
   * `protocol` (readonly): protocol in window.location e.g. 'http:' or 'https:'
   * `port` (readonly): port in window.location e.g. '80'

`headers`
: HTTP request headers for the current session.

`refresh_token`
: The refresh token issued by the OAuth provider to authorize requests to its APIs (if available these are usually longer lived than the `access_token`).

`served`
:Whether we are currently inside a script or notebook that is being served using `panel serve`.

`session_args`
: When running a server session this return the request arguments.

`session_info`
: A dictionary tracking information about server sessions:
   * `total` (int): The total number of sessions that have been opened
   * `live` (int): The current number of live sessions
   * `sessions` (dict(str, dict)): A dictionary of session information:
       * `started`: Timestamp when the session was started
       * `rendered`: Timestamp when the session was rendered
       * `ended`: Timestamp when the session was ended
       * `user_agent`: User-Agent header of client that opened the session

`webdriver`
: Caches the current webdriver to speed up export of bokeh models to PNGs.

`_last_diagnostic_result`
: Stores the diagnostic result from the most recent server startup validation.
  This is kept only for **backward compatibility** — prefer reading per-server
  diagnostics via `server.diagnostic_result` or `state.get_server_status()`.

`_last_startup_config`
: Stores the resolved `StartupConfig` object from the most recent server startup.
  This is kept only for **backward compatibility** — the actual effective
  configuration is now bound to each individual `Server`, `Application`, and
  `SessionContext` instance so that multiple servers or sequential restarts
  never share or overwrite each other's settings. Use `state.get_startup_config(server_id)`
  or `server.startup_config` instead.

## Methods

`add_periodic_callback`
: Schedules a periodic callback to be run at an interval set by the period

`as_cached`
: Allows caching data across sessions by memoizing on the provided key and keyword arguments to the provided function.

`cancel_scheduled`
: Cancel a scheduled task by name.

`execute`
: Executes both synchronous and asynchronous callbacks appropriately depending on the context the application is running in.

`get_startup_config`
: Returns the effective startup configuration. Configuration is bound to
  each individual server, so you can query a specific server or all servers:
  - `state.get_startup_config(server_id)` → config dict for one server, or `None` if not found
  - `state.get_startup_config()` → `{server_id: config_dict}` for all running servers
  Each config dict includes websocket origins, static dirs, admin settings,
  session history, autoreload, notifications, browser info, and detected
  startup mode.

`get_server_status`
: Returns a structured server status dictionary (or JSON string when
  `as_json=True`). Supports querying a specific server or all servers:
  - `state.get_server_status()` → status for all running servers (contains `servers` list)
  - `state.get_server_status(server_id)` → status for one server (contains `server` dict or `error`)
  Each server entry includes:
  - `id`, `address`, `port`: server identity and bind info
  - `panel`: the served application(s) repr
  - `startup`: effective per-service configuration (enabled, config details)
  - `diagnostics`: diagnostic result (if `include_diagnostics=True`)
  Parameters:
  - `server_id` (str, optional): Query a single server instead of all.
  - `include_diagnostics` (bool): Whether to include the diagnostic report.
  - `as_json` (bool): Whether to return a formatted JSON string instead of dict.
  - `indent` (int): JSON indentation level when `as_json=True`.

`kill_all_servers`
: Stops all running server sessions.

`onload`
: Allows defining a callback which is run when a server is fully loaded

`print_server_status`
: Prints a human-readable formatted server status report to stdout.
  Supports printing a single server or all servers.
  Includes startup mode, effective configuration for each service,
  and optionally the diagnostic report.
  Parameters:
  - `server_id` (str, optional): Print status for a single server.
  - `include_diagnostics` (bool): Whether to include the diagnostic report.

`schedule`
: Schedule a callback periodically at a specific time (click [here](../how_to/callbacks/schedule.md) for more details)

`sync_busy`
: Sync an indicator with a boolean value parameter to the busy property on state
