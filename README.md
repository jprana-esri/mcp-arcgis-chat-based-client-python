# ALP Chat Map App (FastAPI, ArcGIS Maps SDK for JavaScript 5.1)

Chat-first map app. A FastAPI backend classifies each message, calls **MCP for ArcGIS Location Services**, and returns answer text plus a GeoJSON FeatureCollection. The browser draws that GeoJSON with the **ArcGIS Maps SDK for JavaScript** (version **5.1**, loaded from the CDN as an ES module with the `<arcgis-map>` map component).

## Requirements
- **Python 3.10 or later**
- An MCP server URL and credentials for ArcGIS Location Services
- An ArcGIS API key

## Setup
```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # fastapi, uvicorn, httpx, jinja2
cp mcp.example.json mcp.json       # then edit mcp.json
```
Run (exact command):
```bash
uvicorn app:app --host 0.0.0.0 --port 3000
```
or
```bash
python app.py
```

Then open http://localhost:3000.

## Configuration
**`mcp.json` location:** put it next to `app.py`. The app falls back to `../mcp.json`. Keep it out of version control.
```json
{
  "servers": {
    "alp-mcp-server": {
      "type": "http",
      "url": "https://YOUR-MCP-HOST/mcp",
      "headers": { "Authorization": "Bearer ${ALP_MCP_TOKEN}" }
    }
  },
  "app": { "mapApiKey": "${MAP_API_KEY}" }
}
```
- `servers.alp-mcp-server.url` and `headers` are the **MCP credentials**. `${ALP_MCP_TOKEN}` placeholders are expanded, so the token can stay in the environment.
- A missing, malformed or incomplete `mcp.json` is logged at startup and returned as a readable error on API requests. The MCP session is initialized at startup, or lazily on first use, then reused.

**ArcGIS Basemap API key (separate from the MCP token)**
1. Sign in at https://location.arcgis.com or https://www.arcgis.com (an ArcGIS Location Platform or ArcGIS Online account).
2. Go to **Credentials → API key credentials → Create API key**.
3. Enable the **Basemaps** service. Restrict the key to your site's referrer, such as `http://localhost:3000`.
4. Configure it with `MAP_API_KEY` in the `mcp.json`.

Browser keys are visible to anyone using the page by design, so restrict them and never reuse the MCP bearer token here. Without a key, chat still works and the map panel shows a message.

| Variable | Purpose |
|---|---|
| `PORT` | HTTP port (default 3000) |
| `MAP_API_KEY` in `mcp.json` | for display ArcGIS Basemap |
| `${ALP_MCP_TOKEN}` in `mcp.json` | for ArcGIS Location Platform MCP |

## How requests are interpreted
The default implementation is **deterministic and rule-based**. No external LLM is used, and answers are generated from templates. Before any tool runs, the server classifies the message as `geocode`, `reverse`, `route`, `elevation` or `unsupported`. The choice is returned in `meta.classification`, and only the tools that workflow needs are called. Unusual phrasing may be misclassified, and the help text then explains what is supported.

## Map SDK notes (version 5.1)
- `templates/index.html` loads the SDK with `<script type="module" src="https://js.arcgis.com/5.1/"></script>`. The old AMD `require([...])` pattern from 4.x is not used.
- The map is the `<arcgis-map basemap="arcgis/navigation">` component. `static/app.js` is an ES module that loads classes with `$arcgis.import("@arcgis/core/...")` and waits for `viewElement.viewOnReady()`.
- The API key reaches the SDK through the global `esriConfig`, which `GET /arcgis-config.js` sets. That script is loaded before the SDK script, so keep that order.
- Clicks use the `arcgisViewClick` event, and extent fitting uses `viewElement.goTo(...)`.
- Reference: https://developers.arcgis.com/javascript/latest/

## HTTP routes
| Route | Purpose |
|---|---|
| `GET /` | chat and map page |
| `GET /api/health` | status, plus whether the MCP config and map key are set |
| `GET /api/tools` | discovered MCP tools |
| `GET /api/arcgis-token` | `{"apiKey": "..."}`: the browser map key, never the MCP token |
| `GET /arcgis-config.js` | `window.ARCGIS_CONFIG` for the frontend |
| `POST /api/chat` | `{message, history?, context?, requestId?}` → `{text, geojson, toolCalls, meta, requestId}` |
| `GET /api/chat/stream` | SSE with events `status`, `tool_call`, `result`, `error`, `done` |

- `history` is `[{role: "user"|"assistant", content}]`.
- `context` is `{selectedPoint: {x, y}, mapExtent: {xmin, ymin, xmax, ymax}}`. Coordinates are in WGS84 degrees, and `x` is longitude. The extent biases geocoding.
- Errors always use `{error: {message, code, details?}}`: 4xx for invalid input, 429 for rate limits, 5xx for upstream MCP failures. Transient failures (network errors, 429, 502, 503, 504) are retried twice with backoff, and persistent ones are surfaced.

**Streaming:** the UI uses **both**. It opens `GET /api/chat/stream?requestId=<id>` for friendly progress text, then calls `POST /api/chat` with the same `requestId` and renders the direct JSON response. The stream's `result` event carries the same payload. The stream also works standalone with `?message=…&history=<json>&context=<json>`, for short requests only.

## Behavior notes
- **Geocode:** the best candidate is mapped, and `meta.geocode` holds the chosen candidate and up to 5 candidates. The UI lists the others under "Other options", and picking one redraws the map.
- **Route:** the result is a `LineString` plus start, end and stop `Point` features. The time is assumed to be in minutes, and distance units are shown only when the response key names them.
- **Coordinates in chat** are read as `lat, lon`. A pair like `-117.19, 34.06` is detected as `lon, lat` and swapped.
- **Replace or append:** a new result replaces the previous graphics. Phrases like "keep previous results" set `meta.append: true`. Text-only responses leave the map unchanged.
- **History:** the server keeps the last 10 turns (each trimmed to 1500 characters) and summarizes older ones. The browser keeps the conversation in `sessionStorage`. Prompts over 2000 characters get a 413 error.

## Example prompts and expected behavior
| Prompt | Behavior |
|---|---|
| `Find Esri headquarters in Redlands` | geocode: best match plotted and zoomed to, other candidates listed |
| `What's at 34.0601, -117.1964?` | reverse: nearest address in the reply, point plotted |
| `Route from Union Station to LAX` | route: both places geocoded, route solved and drawn, summary and directions in the reply |
| `Walk from 34.05, -118.25 to 34.06, -118.24` | route on foot between the two coordinates |
| `Elevation at 36.5785, -118.2923` | elevation in meters, point plotted. A place name like `How high is Mount Whitney?` is geocoded first. Points must fit within 50 km |
| Click the map, then `Address here` or `Elevation here` | uses the selected point |
| `Find Santa Monica Pier and keep the previous results` | result is added to the map instead of replacing it |
| `Show a 10 minute drive-time area around 34.05, -118.25` | unsupported: friendly explanation, no tool called, `geojson: null`, map unchanged |
