"""Chat-first map app: FastAPI + MCP for ArcGIS Location Services (rule-based, no external LLM)."""
import asyncio
import json
import logging
import math
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Literal, Optional

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("alp-chat")
BASE = Path(__file__).resolve().parent
SERVER_NAME = "alp-mcp-server"
MAX_PROMPT, MAX_TURNS, MAX_TURN_CHARS = 2000, 10, 1500
RID = re.compile(r"^[\w-]{8,64}$")


# ---------------- errors ----------------
class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str = "ERROR", details: Any = None):
        super().__init__(message)
        self.status, self.message, self.code, self.details = status, message, code, details

    def body(self) -> dict:
        b = {"message": self.message, "code": self.code}
        if self.details is not None:
            b["details"] = self.details
        return {"error": b}


def err_json(status, message, code, details=None):
    return JSONResponse(ApiError(status, message, code, details).body(), status_code=status)


def is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


# ---------------- config ----------------
def _sub(v) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), str(v if v is not None else ""))


def read_config_file() -> tuple:
    for p in (BASE / "mcp.json", BASE.parent / "mcp.json"):
        if p.is_file():
            try:
                data = json.loads(p.read_text("utf-8"))
            except (OSError, ValueError) as e:
                raise ApiError(500, f"{p} is not valid JSON: {e}", "CONFIG_ERROR")
            if not isinstance(data, dict):
                raise ApiError(500, f"{p} must contain a JSON object", "CONFIG_ERROR")
            return data, p
    raise ApiError(500, "mcp.json not found in ./ or ../. Copy mcp.example.json to mcp.json and set the MCP URL and headers.", "CONFIG_ERROR")


def load_mcp_config() -> dict:
    data, p = read_config_file()
    srv = (data.get("servers") or {}).get(SERVER_NAME)
    if not isinstance(srv, dict) or not srv.get("url"):
        raise ApiError(500, f"{p} is missing servers.{SERVER_NAME}.url", "CONFIG_ERROR")
    return {"url": _sub(srv["url"]), "file": str(p), "headers": {k: _sub(v) for k, v in (srv.get("headers") or {}).items()}}


def map_api_key() -> Optional[str]:
    """Browser credential, separate from the MCP bearer token."""
    key = os.environ.get("MAP_API_KEY") or os.environ.get("ARCGIS_API_KEY")
    if key:
        return key
    try:
        return _sub(((read_config_file()[0].get("app") or {}).get("mapApiKey"))) or None
    except ApiError:
        return None


# ---------------- reusable MCP client ----------------
TRANSIENT = {429, 502, 503, 504}


def parse_payload(r: httpx.Response, req_id: int) -> Optional[dict]:
    """JSON or SSE; for SSE use the last valid JSON-RPC payload."""
    text = r.text.strip()
    if not text:
        return None
    found: list = []
    if "text/event-stream" in r.headers.get("content-type", "") or not text.startswith(("{", "[")):
        for event in re.split(r"\r?\n\r?\n", text):
            data = "\n".join(l[5:].lstrip() for l in event.splitlines() if l.startswith("data:"))
            if data:
                try:
                    found.append(json.loads(data))
                except ValueError:
                    pass
    else:
        try:
            found.append(json.loads(text))
        except ValueError:
            raise ApiError(502, "The MCP server sent an unreadable response.", "MCP_PARSE")
    flat = [m for p in found for m in (p if isinstance(p, list) else [p]) if isinstance(m, dict) and ("result" in m or "error" in m)]
    pick = [m for m in flat if m.get("id") == req_id] or flat
    return pick[-1] if pick else None


class MCPClient:
    def __init__(self):
        self._http: Optional[httpx.AsyncClient] = None
        self._cfg: dict = {}
        self._sid = self._ver = None
        self._ready = False
        self._lock = asyncio.Lock()
        self._id = 0

    def _next(self) -> int:
        self._id += 1
        return self._id

    async def _post(self, body: dict, session: bool = True) -> httpx.Response:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **self._cfg["headers"]}
        if session and self._sid:
            headers["Mcp-Session-Id"] = self._sid
        if session and self._ver:
            headers["MCP-Protocol-Version"] = self._ver
        last: Optional[ApiError] = None
        for attempt in range(3):
            delay = 0.4 * 2 ** attempt
            try:
                r = await self._http.post(self._cfg["url"], headers=headers, json=body)
            except httpx.HTTPError as e:
                last = ApiError(502, f"Could not reach the MCP server ({e.__class__.__name__}). Check the URL in mcp.json and your network.", "MCP_UNAVAILABLE")
                await asyncio.sleep(delay)
                continue
            if r.status_code < 400:
                return r
            code = r.status_code
            last = ApiError(
                429 if code == 429 else 502,
                "The map service is rate limiting requests. Wait a moment and try again." if code == 429
                else "The MCP server rejected the credentials. Check the headers in mcp.json." if code in (401, 403)
                else f"The MCP server returned HTTP {code}.",
                "RATE_LIMITED" if code == 429 else "MCP_AUTH" if code in (401, 403) else "MCP_HTTP",
                {"status": code, "body": r.text[:300]})
            if code not in TRANSIENT:
                raise last
            try:
                delay = min(float(r.headers.get("retry-after", delay)), 5)
            except ValueError:
                pass
            await asyncio.sleep(delay)
        raise last  # persistent failure: surface it

    async def ensure(self) -> None:
        async with self._lock:
            if self._ready:
                return
            self._cfg = load_mcp_config()
            if self._http is None:
                self._http = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15))
            rid = self._next()
            r = await self._post({"jsonrpc": "2.0", "id": rid, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "alp-chat-map-app", "version": "1.0.0"}}}, session=False)
            msg = parse_payload(r, rid)
            if not msg or "error" in msg:
                raise ApiError(502, "MCP initialize failed: " + ((msg or {}).get("error") or {}).get("message", "no response"), "MCP_INIT")
            res = msg.get("result") or {}
            self._sid, self._ver = r.headers.get("mcp-session-id"), res.get("protocolVersion", "2025-06-18")
            try:
                await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            except ApiError:
                pass
            self._ready = True

    async def request(self, method: str, params: Optional[dict] = None, _retry: bool = True) -> dict:
        await self.ensure()
        rid = self._next()
        try:
            r = await self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        except ApiError as e:
            if _retry and isinstance(e.details, dict) and e.details.get("status") == 404:
                self._ready = False
                return await self.request(method, params, False)
            raise
        msg = parse_payload(r, rid)
        if msg is None:
            raise ApiError(502, "The MCP server returned an empty response.", "MCP_EMPTY")
        if "error" in msg:
            err = msg["error"] or {}
            raise ApiError(502, err.get("message", "MCP error"), "MCP_RPC", err)
        return msg.get("result") or {}

    async def list_tools(self) -> list:
        res = await self.request("tools/list")
        return [{"name": t.get("name"), "description": (t.get("description") or "").split("\n")[0]} for t in res.get("tools", [])]

    async def call_tool(self, name: str, arguments: dict) -> dict:
        res = await self.request("tools/call", {"name": name, "arguments": arguments})
        text = "\n".join(c.get("text", "") for c in (res.get("content") or []) if c.get("type") == "text")
        if res.get("isError"):
            raise ApiError(502, text or f"The {name} tool reported an error.", "TOOL_ERROR", {"tool": name})
        data = res.get("structuredContent")
        if data is None and text:
            try:
                data = json.loads(text)
            except ValueError:
                data = {"text": text}
        return data if isinstance(data, dict) else ({"items": data} if data is not None else {})

    async def close(self):
        if self._http:
            await self._http.aclose()


mcp = MCPClient()


# ---------------- parsing & classification (rule-based) ----------------
UNSUPPORTED = re.compile(r"\b(drive[- ]?time|travel[- ]?time (?:area|polygon)|service[- ]area|isochrone|catchment|within \d+\s*(?:min|minute|minutes|hour|hours)|closest facility|nearest (?:facility|facilities)|optimi[sz]e|optimal order|reorder (?:the )?stops|fleet|origin[- ]destination|od[- ]matrix|salesman|buffer)\b", re.I)
HELP = re.compile(r"^\s*(hi|hello|hey|help|thanks|thank you|what can you do)\b", re.I)
APPEND = re.compile(r"(?:,?\s*(?:and\s+)?(?:please\s+)?(?:keep|add)\b[^.?!]*(?:previous|prior|existing|earlier|last|results?|map)[^.?!]*)|\b(?:in addition|also (?:show|plot|add)|along with (?:the )?previous|without clearing)\b", re.I)
HERE = re.compile(r"\b(here|this (?:location|point|spot|place)|selected (?:point|location)|where i clicked|that point|there)\b", re.I)
HELP_TEXT = ('I can help with four things:\n• Find a place or address, e.g. "Find Esri headquarters in Redlands"\n'
             '• Identify a location, e.g. "What\'s at 34.0601, -117.1964?"\n• Route between places, e.g. "Route from Union Station to LAX"\n'
             '• Elevation, e.g. "Elevation at 36.5785, -118.2923"')


def parse_coords(t: str) -> list:
    pairs = [(float(m[1]), float(m[2])) for m in re.finditer(r"(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)", t)]
    if not pairs:
        pairs = [(float(m[1]), float(m[2])) for m in re.finditer(r"(-?\d{1,3}\.\d+)\s+(-?\d{1,3}\.\d+)", t)]
    out = []
    for a, b in pairs:  # text order is "lat, lon"; swap if clearly "lon, lat"
        y, x = (b, a) if abs(a) > 90 >= abs(b) else (a, b)
        if abs(y) <= 90 and abs(x) <= 180:
            out.append({"x": x, "y": y})
    return out


def classify(t: str, has_ref: bool) -> dict:
    if HELP.match(t) and len(t.split()) <= 6:
        return {"type": "unsupported", "reason": "help"}
    if UNSUPPORTED.search(t):
        return {"type": "unsupported", "reason": "unsupported_workflow"}
    coords = parse_coords(t)
    if re.search(r"\b(elevation|altitude|how high|above sea level|height above)\b", t, re.I):
        return {"type": "elevation"}
    if re.search(r"\b(route|routes|directions?|navigate|drive|driving|walk|walking|how (?:do|can) i get)\b", t, re.I) and re.search(r"\bto\b", t, re.I):
        return {"type": "route"}
    if re.search(r"\b(reverse|address (?:of|at|for)|what(?:'s| is) (?:the address|at)|what address|whats at)\b", t, re.I) or \
            ((coords or (HERE.search(t) and has_ref)) and not re.search(r"\b(find|geocode|locate)\b", t, re.I)):
        return {"type": "reverse"}
    if len(t.split()) > 12 and not re.search(r"\b(find|where|locate|geocode|show|map|plot|search)\b", t, re.I):
        return {"type": "unsupported", "reason": "unclear"}
    return {"type": "geocode"}


# ---------------- request models & validation ----------------
class XY(BaseModel):
    x: float = Field(ge=-180, le=180)
    y: float = Field(ge=-90, le=90)


class Extent(BaseModel):
    xmin: float
    ymin: float
    xmax: float
    ymax: float


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    point: Optional[XY] = None


class ChatContext(BaseModel):
    selectedPoint: Optional[XY] = None
    mapExtent: Optional[Extent] = None


class ChatReq(BaseModel):
    message: str
    history: list[ChatTurn] = Field(default_factory=list)
    context: Optional[ChatContext] = None
    requestId: Optional[str] = Field(None, pattern=r"^[\w-]{8,64}$")


def prepare(req: ChatReq) -> dict:
    message = req.message.strip()
    if not message:
        raise ApiError(400, "Type a message to get started.", "EMPTY_MESSAGE")
    if len(message) > MAX_PROMPT:
        raise ApiError(413, f"That message is too long ({len(message)} characters). Keep it under {MAX_PROMPT}.", "PROMPT_TOO_LARGE")
    hist = req.history[-200:]
    older, recent = hist[:-MAX_TURNS], hist[-MAX_TURNS:]
    asked = [h.content[:60] for h in older if h.role == "user"][-5:]
    ctx = req.context or ChatContext()
    ex = ctx.mapExtent
    extent = {"xmin": ex.xmin, "ymin": ex.ymin, "xmax": ex.xmax, "ymax": ex.ymax} if ex and ex.xmin < ex.xmax and ex.ymin < ex.ymax \
        and abs(ex.ymin) <= 90 and abs(ex.ymax) <= 90 else None
    return {
        "message": message,
        "turns": len(recent),
        "summary": f"Earlier in this chat ({len(older)} turns trimmed), the user asked: " + " | ".join(asked) if older else None,
        "last_point": next(({"x": h.point.x, "y": h.point.y} for h in reversed(hist) if h.point), None),
        "selected": {"x": ctx.selectedPoint.x, "y": ctx.selectedPoint.y} if ctx.selectedPoint else None,
        "extent": extent,
    }


# ---------------- geometry helpers ----------------
def fmt(p) -> str:
    return f"{p['y']:.5f}, {p['x']:.5f}"


def point_feature(p, props):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [p["x"], p["y"]]}, "properties": props}


def fc(features):
    return {"type": "FeatureCollection", "features": features} if features else None


def find_path(o, depth=0):
    if depth > 6 or not isinstance(o, (dict, list)):
        return None
    if isinstance(o, dict):
        if isinstance(o.get("paths"), list):
            return [q for path in o["paths"] for q in path]
        c = o.get("coordinates")
        if isinstance(c, list) and c and isinstance(c[0], list) and c[0] and is_num(c[0][0]):
            return c
    for v in (o.values() if isinstance(o, dict) else o):
        r = find_path(v, depth + 1)
        if r:
            return r
    return None


def find_key(o, names, depth=0, want_array=False):
    if depth > 6:
        return None
    if isinstance(o, dict):
        for k, v in o.items():
            if k.lower() in names and (isinstance(v, list) if want_array else (isinstance(v, (int, float, str)) and not isinstance(v, bool))):
                return k, v
        for v in o.values():
            r = find_key(v, names, depth + 1, want_array)
            if r:
                return r
    elif isinstance(o, list):
        for v in o[:20]:
            r = find_key(v, names, depth + 1, want_array)
            if r:
                return r
    return None


def find_elev_arr(o, depth=0):
    if depth > 5:
        return None
    if isinstance(o, list) and o and all(is_num(v) or (isinstance(v, dict) and any(k in v for k in ("z", "elevation", "elev"))) for v in o):
        return o
    if isinstance(o, (dict, list)):
        for v in (o.values() if isinstance(o, dict) else o):
            r = find_elev_arr(v, depth + 1)
            if r:
                return r
    return None


def hm(minutes: float) -> str:
    h, m = int(minutes // 60), round(minutes % 60)
    return f"{h} h {m} min" if h else f"{m} min"


# ---------------- chat orchestration ----------------
async def run_chat(inp: dict, emit: Callable[[str, dict], None]) -> dict:
    message, calls = inp["message"], []
    ref = inp["selected"] or inp["last_point"]
    append = bool(APPEND.search(message))
    cleaned = APPEND.sub("", message).strip()
    cls = classify(cleaned, bool(ref))
    meta = {"classification": cls["type"], "append": append, "history": {"kept": inp["turns"], "summarized": bool(inp["summary"])}}
    emit("status", {"text": "Understanding your request…", "classification": cls["type"]})

    def done(text, geojson=None, **extra):
        return {"text": text, "geojson": geojson, "toolCalls": calls, "meta": {**meta, **extra}}

    async def tool(name, args):
        emit("tool_call", {"name": name, "args": json.dumps(args)[:200]})
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        try:
            data = await mcp.call_tool(name, args)
            calls.append({"name": name, "ok": True, "ms": round((loop.time() - t0) * 1000)})
            return data
        except ApiError as e:
            calls.append({"name": name, "ok": False, "ms": round((loop.time() - t0) * 1000), "error": e.message})
            raise

    async def geocode_one(q):
        args = {"singleLine": q}
        if inp["extent"]:
            args["searchExtent"] = {**inp["extent"], "spatialReference": {"wkid": 4326}}
        d = await tool("find_address_candidates", args)
        raw = d.get("candidates") or (d.get("result") or {}).get("candidates") or d.get("items") or []
        out = []
        for c in raw:
            loc = c.get("location") or c.get("geometry") or {}
            if is_num(loc.get("x")) and is_num(loc.get("y")):
                out.append({"address": c.get("address") or (c.get("attributes") or {}).get("LongLabel") or "",
                            "x": loc["x"], "y": loc["y"], "score": c.get("score")})
        return out

    async def resolve(s):
        t = re.sub(r"[.?!]+$", "", s.strip())
        if HERE.search(t) and ref:
            return {**ref, "name": "the selected point"}
        c = (parse_coords(t) or [None])[0]
        if c:
            return {**c, "name": fmt(c)}
        emit("status", {"text": f'Looking up "{t}"…'})
        best = (await geocode_one(t) or [None])[0]
        return {"x": best["x"], "y": best["y"], "name": best["address"] or t} if best else None

    if cls["type"] == "unsupported":
        r = cls["reason"]
        return done(f"Hi! {HELP_TEXT}" if r == "help" else
                    f"I can't do that one here. Drive-time areas, service areas, buffers, stop optimization and similar analysis aren't available through this chat. {HELP_TEXT}"
                    if r == "unsupported_workflow" else f"I'm not sure how to map that request. {HELP_TEXT}")

    if cls["type"] == "geocode":
        q = re.sub(r"^(?:please\s+)?(?:find|locate|geocode|show(?:\s+me)?|where(?:'s|\s+is|\s+are)|search\s+for|look\s+up|map|plot|coordinates\s+(?:of|for)|what(?:'s|\s+is)\s+the\s+location\s+of)\s+", "", cleaned, flags=re.I)
        q = re.sub(r"\s+on\s+the\s+map\b", "", q, flags=re.I)
        q = re.sub(r"[?.!]+$", "", q).strip()
        if not q:
            return done("Which place or address should I look up?")
        emit("status", {"text": "Searching for matching places…"})
        cands = await geocode_one(q)
        if not cands:
            return done(f'I couldn\'t find a match for "{q}". Try adding a city or country.', geocodeCandidates=0)
        top = cands[:5]
        best = top[0]
        feat = point_feature(best, {"kind": "geocode", "title": best["address"] or q, "address": best["address"], "score": best["score"], "candidateCount": len(cands)})
        more = "\n".join(f"• {c['address']} ({fmt(c)})" for c in top[1:3])
        text = (f'I found {len(cands)} match{"es" if len(cands) > 1 else ""} for "{q}" and chose the top one: {best["address"]} ({fmt(best)}).'
                + (f"\nOther candidates:\n{more}" if more else ""))
        return done(text, fc([feat]), geocodeCandidates=len(cands), geocode={"query": q, "count": len(cands), "chosen": best, "candidates": top})

    if cls["type"] == "reverse":
        c = (parse_coords(cleaned) or [None])[0] or (ref if HERE.search(cleaned) else None)
        if not c:
            return done('Give me a coordinate like "34.0601, -117.1964", or click the map to select a point first.')
        emit("status", {"text": "Looking up the address…"})
        d = await tool("reverse_geocode", {"x": c["x"], "y": c["y"]})
        addr = d.get("Address") or d.get("address") or ""
        if not addr:
            return done(f"I couldn't find an address near {fmt(c)}.", fc([point_feature(c, {"kind": "reverse", "title": fmt(c)})]))
        lt = f" ({d['LocationType']})" if d.get("LocationType") else ""
        return done(f"The nearest address to {fmt(c)} is {addr}{lt}.",
                    fc([point_feature(c, {"kind": "reverse", "title": addr, "address": addr, "city": d.get("City"), "region": d.get("Region"),
                                          "country": d.get("CountryCode"), "locationType": d.get("LocationType")})]), address=addr)

    if cls["type"] == "elevation":
        pts = [{**c, "name": fmt(c)} for c in parse_coords(cleaned)[:100]]
        if not pts and HERE.search(cleaned) and ref:
            pts = [{**ref, "name": fmt(ref)}]
        if not pts:
            m = re.search(r"how high (?:is|are)\s+(.+)", cleaned, re.I)
            place = m.group(1) if m else re.sub(r"^(?:please\s+)?(?:what(?:'s|\s+is)|tell\s+me|get|find|show)?\s*(?:me\s+)?(?:the\s+)?(?:elevation|altitude)\s*(?:of|at|for|in)?\s*", "", cleaned, flags=re.I)
            if not place.strip():
                return done('Which location? Give a place name, coordinates like "36.5785, -118.2923", or click the map.')
            p = await resolve(place)
            if not p:
                return done(f'I couldn\'t find "{place.strip()}" to look up its elevation.')
            pts = [p]
        ys, xs = [p["y"] for p in pts], [p["x"] for p in pts]
        mid = math.radians((min(ys) + max(ys)) / 2)
        if (max(xs) - min(xs)) * 111.32 * math.cos(mid) > 50 or (max(ys) - min(ys)) * 111.32 > 50:
            return done("Those points are more than 50 km apart east-west or north-south, which the elevation service doesn't allow in one request. Try a closer group of points.")
        emit("status", {"text": "Checking elevation…"})
        d = await tool("elevation_at_locations", {"coordinates": [{"x": p["x"], "y": p["y"]} for p in pts]})
        arr = find_elev_arr(d) or []
        vals = []
        for i in range(len(pts)):
            v = arr[i] if i < len(arr) else None
            if isinstance(v, dict):
                v = next((v[k] for k in ("z", "elevation", "elev") if is_num(v.get(k))), None)
            vals.append(v if is_num(v) else None)
        feats = [point_feature(p, {"kind": "elevation", "title": p["name"] if vals[i] is None else f"{vals[i]} m", "elevation": vals[i], "unit": "m", "label": p["name"]})
                 for i, p in enumerate(pts)]
        if all(v is None for v in vals):
            text = "The service responded but I couldn't read elevation values from it."
        else:
            lines = "\n".join(f"• {p['name']}: {'not available' if vals[i] is None else f'{vals[i]} m above mean sea level'}" for i, p in enumerate(pts[:10]))
            text = f"Elevation (meters, relative to mean sea level):\n{lines}" + (f"\n…and {len(pts) - 10} more" if len(pts) > 10 else "")
        return done(text, fc(feats), elevationPoints=len(pts))

    # route
    body = re.sub(r"[?.!]+$", "", cleaned)
    mode = "Driving Time"
    if re.search(r"\b(walk|walking|on foot)\b", body, re.I):
        mode = "Walking Time"
    elif re.search(r"\b(truck|trucking)\b", body, re.I):
        mode = "Trucking Time"
    elif re.search(r"\bshortest\b", body, re.I):
        mode = "Driving Distance"
    body = re.sub(r"\b(?:by|on)\s+(?:foot|walking|truck|car)\b|\bshortest\b", "", body, flags=re.I).strip()
    sp = re.search(r"\b(?:via|through|stopping at)\s+(.+)$", body, re.I)
    if sp:
        body = body[:sp.start()].strip()
    m = re.search(r"\bfrom\s+(.+?)\s+to\s+(.+)$", body, re.I) or re.match(r"^(?:please\s+)?(?:route|directions?|navigate|drive|walk)\s+(.+?)\s+to\s+(.+)$", body, re.I)
    if m:
        start_txt, end_txt = m.group(1), m.group(2)
    else:
        m = re.search(r"\b(?:route|directions?|navigate|drive|walk)\s+to\s+(.+)$", body, re.I)
        if not m:
            return done('Tell me where to start and finish, e.g. "Route from Union Station to LAX".')
        start_txt, end_txt = "here", m.group(1)
    if start_txt == "here" and not ref:
        return done('Where should the route start? Say "from <place>" or click the map to select a start point.')
    stop_txts = [s for s in re.split(r"\s*(?:;|\bthen\b|\band\b)\s*", sp.group(1), flags=re.I) if s][:10] if sp else []
    start, end = await resolve(start_txt), await resolve(end_txt)
    if not start or not end:
        return done(f'I couldn\'t find "{start_txt if not start else end_txt}". Try a fuller place name or coordinates.')
    stops = []
    for s in stop_txts:
        p = await resolve(s)
        if not p:
            return done(f'I couldn\'t find the stop "{s}".')
        stops.append(p)
    emit("status", {"text": "Solving the route…"})
    xy = lambda p: {"x": p["x"], "y": p["y"]}
    args = {"start": xy(start), "end": xy(end), "travelMode": mode, "responseType": "all", "simplifyRouteForMapDisplay": False}
    if stops:
        args["stops"] = [xy(s) for s in stops]
    d = await tool("solve_route", args)
    path = [[q[0], q[1]] for q in (find_path(d) or []) if isinstance(q, (list, tuple)) and len(q) >= 2 and is_num(q[0]) and is_num(q[1])]
    tm = find_key(d, {"totaltime", "total_time", "traveltime", "duration"})
    dist = find_key(d, {"totalkilometers", "totalmiles", "totallength", "total_length", "totaldistance", "total_distance", "distance", "length"})
    unit = (" km" if re.search("kilometer", dist[0], re.I) else " mi" if re.search("mile", dist[0], re.I) else "") if dist else ""
    dirs_raw = (find_key(d, {"directions"}, want_array=True) or (None, []))[1]
    dirs = [x if isinstance(x, str) else (x.get("text") or x.get("instruction") or x.get("description")) for x in dirs_raw if x]
    dirs = [x for x in dirs if x]
    summary = {"travelMode": mode, "totalTime": tm[1] if tm else None, "totalDistance": dist[1] if dist else None, "distanceUnit": unit or None}
    head = f"Route from {start['name']} to {end['name']}" + (f" via {len(stops)} stop{'s' if len(stops) > 1 else ''}" if stops else "") + f" ({mode})."
    bits = []
    if tm and is_num(tm[1]):
        bits.append(f"about {hm(float(tm[1]))}")
    if dist:
        bits.append(f"{dist[1]}{unit}")
    stats = ", ".join(bits)
    steps = ("\nDirections:\n" + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(dirs[:25])) + (f"\n…and {len(dirs) - 25} more steps" if len(dirs) > 25 else "")) if dirs else ""
    text = head + (" " + stats[0].upper() + stats[1:] + "." if stats else "") + steps
    if len(path) < 2:
        return done(f"{head} The route was solved, but it returned no drawable geometry.{steps}", routeSummary=summary)
    feats = [{"type": "Feature", "geometry": {"type": "LineString", "coordinates": path},
              "properties": {"kind": "route", "title": f"{start['name']} to {end['name']}", **summary}},
             point_feature(start, {"kind": "route-start", "title": start["name"]}), point_feature(end, {"kind": "route-end", "title": end["name"]}),
             *[point_feature(s, {"kind": "route-stop", "title": s["name"], "stop": i + 1}) for i, s in enumerate(stops)]]
    return done(text, fc(feats), routeSummary=summary, vertices=len(path))


# ---------------- SSE job bus ----------------
class Job:
    def __init__(self):
        self.log: list = []
        self.queues: set = set()
        self.done = False


jobs: dict = {}


def get_job(rid: str) -> Job:
    j = jobs.get(rid)
    if j is None:
        j = jobs[rid] = Job()
        asyncio.get_running_loop().call_later(120, jobs.pop, rid, None)
    return j


def publish(rid: str, ev: str, data: dict) -> None:
    j = get_job(rid)
    j.log.append((ev, data))
    j.done = j.done or ev == "done"
    for q in j.queues:
        q.put_nowait((ev, data))


def sse(ev: str, data: dict) -> str:
    return f"event: {ev}\ndata: {json.dumps(data)}\n\n"


async def stream_job(rid: str):
    j = get_job(rid)
    q: asyncio.Queue = asyncio.Queue()
    snapshot = list(j.log)
    j.queues.add(q)          # no await between snapshot and subscribe, so no event is missed
    try:
        for ev, data in snapshot:
            yield sse(ev, data)
        if j.done:
            return
        while True:
            try:
                ev, data = await asyncio.wait_for(q.get(), 15)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield sse(ev, data)
            if ev == "done":
                return
    finally:
        j.queues.discard(q)


async def execute(rid: str, inp: dict) -> dict:
    emit = lambda ev, data: publish(rid, ev, data)
    try:
        out = await run_chat(inp, emit)
    except ApiError as e:
        publish(rid, "error", e.body()["error"])
        publish(rid, "done", {})
        raise
    out["requestId"] = rid
    publish(rid, "result", out)
    publish(rid, "done", {})
    return out


# ---------------- app ----------------
@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        load_mcp_config()
        await mcp.ensure()
        log.warning("MCP session ready")
    except ApiError as e:
        log.warning("MCP not ready at startup: %s (will retry on first request)", e.message)
    if not map_api_key():
        log.warning("No browser map key set (MAP_API_KEY / ARCGIS_API_KEY / app.mapApiKey): the map will not load.")
    yield
    await mcp.close()


app = FastAPI(title="ALP Chat Map", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))


@app.exception_handler(ApiError)
async def _api_error(_: Request, e: ApiError):
    return JSONResponse(e.body(), status_code=e.status)


@app.exception_handler(RequestValidationError)
async def _validation(_: Request, e: RequestValidationError):
    details = [{"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]} for err in e.errors()]
    return err_json(422, "The request was invalid: " + "; ".join(f"{d['field']}: {d['message']}" for d in details[:3]), "INVALID_REQUEST", details)


@app.exception_handler(StarletteHTTPException)
async def _http(_: Request, e: StarletteHTTPException):
    return err_json(e.status_code, "Unknown endpoint." if e.status_code == 404 else str(e.detail), "NOT_FOUND" if e.status_code == 404 else "HTTP_ERROR")


@app.exception_handler(Exception)
async def _unhandled(_: Request, e: Exception):
    log.exception("Unhandled error")
    return err_json(500, "Something went wrong on the server. Check the server logs.", "INTERNAL_ERROR")


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


@app.get("/api/health")
async def health():
    out = {"status": "ok", "port": int(os.environ.get("PORT", 3000)), "mapKeyConfigured": bool(map_api_key())}
    try:
        load_mcp_config()
        out["mcpConfigured"] = True
    except ApiError as e:
        out.update(mcpConfigured=False, configError=e.message)
    return out


@app.get("/api/tools")
async def tools():
    return {"tools": await mcp.list_tools()}


@app.get("/api/arcgis-token")
async def arcgis_token():
    key = map_api_key()
    if not key:
        raise ApiError(503, "No browser map API key is configured. Set MAP_API_KEY (or ARCGIS_API_KEY), or app.mapApiKey in mcp.json.", "MAP_KEY_MISSING")
    return JSONResponse({"apiKey": key}, headers={"Cache-Control": "no-store"})


@app.get("/arcgis-config.js")
async def arcgis_config():
    key = map_api_key()
    cfg = {"apiKey": key, "hasKey": bool(key), "basemap": "arcgis/navigation", "tokenEndpoint": "/api/arcgis-token"}
    js = f"window.ARCGIS_CONFIG={json.dumps(cfg)};"
    if key:  # the SDK reads the global esriConfig when it loads, so this script must come before the SDK script
        js += f"window.esriConfig={json.dumps({'apiKey': key})};"
    return Response(js, media_type="application/javascript", headers={"Cache-Control": "no-store"})


@app.post("/api/chat")
async def chat(req: ChatReq):
    inp = prepare(req)
    return await execute(req.requestId or uuid.uuid4().hex, inp)


@app.get("/api/chat/stream")
async def chat_stream(requestId: Optional[str] = Query(None), message: Optional[str] = Query(None),
                      history: Optional[str] = Query(None), context: Optional[str] = Query(None)):
    headers = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}
    if requestId:  # attach to an active POST /api/chat request
        if not RID.match(requestId):
            raise ApiError(400, "Invalid requestId.", "INVALID_REQUEST")
        return StreamingResponse(stream_job(requestId), media_type="text/event-stream", headers=headers)
    if message is None:
        raise ApiError(400, 'Provide "requestId" to follow a request, or "message" to run one.', "INVALID_REQUEST")
    try:
        req = ChatReq(message=message, history=json.loads(history) if history else [], context=json.loads(context) if context else None)
    except ValueError as e:
        raise ApiError(400, f'"history" and "context" must be valid JSON matching the chat contract. ({str(e)[:120]})', "INVALID_REQUEST")
    inp = prepare(req)
    rid = uuid.uuid4().hex
    get_job(rid)
    asyncio.create_task(_run_quietly(rid, inp))
    return StreamingResponse(stream_job(rid), media_type="text/event-stream", headers=headers)


async def _run_quietly(rid: str, inp: dict) -> None:
    try:
        await execute(rid, inp)
    except ApiError:
        pass
    except Exception:
        log.exception("stream chat failed")
        publish(rid, "error", {"message": "Something went wrong on the server.", "code": "INTERNAL_ERROR"})
        publish(rid, "done", {})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
