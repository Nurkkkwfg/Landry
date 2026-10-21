import asyncio
import logging
import time
from math import asin, cos, radians, sin, sqrt
from urllib.parse import quote

import httpx

from config import DGIS_KEY

log = logging.getLogger(__name__)

BISHKEK = (42.8746, 74.5698)
_BOX = (42.70, 74.20, 43.10, 74.95)  # lat_min, lon_min, lat_max, lon_max
_UA = {"User-Agent": "laundry-bot/1.0"}
_lock = asyncio.Lock()
_last = 0.0
_client: httpx.AsyncClient | None = None
_cache: dict[str, list[dict]] = {}


def km(lat1, lon1, lat2, lon2) -> float:
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    h = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * 6371.0088 * asin(sqrt(min(1.0, h)))


def map_url(address: str, lat: float | None, lon: float | None) -> str:
    if lat is not None and lon is not None:
        return f"https://2gis.kg/bishkek/geo/{lon},{lat}"
    return f"https://2gis.kg/bishkek/search/{quote(address or '')}"


def _inside(lat: float, lon: float) -> bool:
    return _BOX[0] <= lat <= _BOX[2] and _BOX[1] <= lon <= _BOX[3]


async def _get(url: str, params: dict):
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=6.0, headers=_UA)
    r = await _client.get(url, params=params)
    r.raise_for_status()
    return r.json()


async def _osm_get(url: str, params: dict):
    """Публичные OSM-сервисы: не чаще 1 запроса в секунду."""
    global _last
    async with _lock:
        wait = 1.0 - (time.monotonic() - _last)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            return await _get(url, params)
        finally:
            _last = time.monotonic()


def _add(out: list[dict], label: str, lat, lon) -> None:
    label = " ".join((label or "").split())
    if not label or lat is None or lon is None:
        return
    lat, lon = float(lat), float(lon)
    if _inside(lat, lon) and all(o["label"] != label for o in out):
        out.append({"label": label, "lat": lat, "lon": lon})


async def _dgis(q: str) -> list[dict]:
    data = await _get(
        "https://catalog.api.2gis.com/3.0/items/geocode",
        {
            "q": f"Бишкек, {q}",
            "fields": "items.point,items.full_name,items.address_name",
            "key": DGIS_KEY,
            "page_size": 5,
        },
    )
    out: list[dict] = []
    for it in (data.get("result") or {}).get("items") or []:
        p = it.get("point") or {}
        _add(out, it.get("address_name") or it.get("full_name"), p.get("lat"), p.get("lon"))
    return out


async def _photon(q: str) -> list[dict]:
    data = await _osm_get(
        "https://photon.komoot.io/api/",
        {"q": f"{q}, Бишкек", "limit": 5, "lat": BISHKEK[0], "lon": BISHKEK[1]},
    )
    out: list[dict] = []
    for f in data.get("features") or []:
        pr, (lon, lat) = f.get("properties") or {}, f["geometry"]["coordinates"][:2]
        street, house = pr.get("street"), pr.get("housenumber")
        if street and house:
            label = f"{street} {house}"
        else:
            label = pr.get("name") or street or ""
            if house:
                label += f" {house}"
        if pr.get("district"):
            label += f", {pr['district']}"
        _add(out, label, lat, lon)
    return out


async def _nominatim(q: str) -> list[dict]:
    data = await _osm_get(
        "https://nominatim.openstreetmap.org/search",
        {
            "q": f"{q}, Бишкек",
            "format": "jsonv2",
            "addressdetails": 1,
            "limit": 5,
            "countrycodes": "kg",
            "viewbox": f"{_BOX[1]},{_BOX[2]},{_BOX[3]},{_BOX[0]}",
            "bounded": 1,
        },
    )
    out: list[dict] = []
    for r in data:
        a = r.get("address") or {}
        road = a.get("road") or a.get("pedestrian") or ""
        label = f"{road} {a.get('house_number', '')}".strip() or (r.get("display_name") or "").split(",")[0]
        if a.get("suburb"):
            label += f", {a['suburb']}"
        _add(out, label, r.get("lat"), r.get("lon"))
    return out


async def search(query: str) -> list[dict]:
    """До 5 вариантов адреса [{label, lat, lon}] в Бишкеке. Пусто — если ничего не нашли/сервисы недоступны."""
    key = " ".join(query.lower().split())
    if key in _cache:
        return _cache[key]
    providers = ([_dgis] if DGIS_KEY else []) + [_photon, _nominatim]
    for provider in providers:
        for attempt in (1, 2):  # один повтор при сбое сети
            try:
                res = await asyncio.wait_for(provider(query), 9)
            except Exception as e:
                log.warning("%s (try %s): %s", provider.__name__, attempt, e)
                continue
            if res:
                if len(_cache) > 500:
                    _cache.clear()
                _cache[key] = res
                return res
            break  # ответ без результатов — пробуем следующий сервис
    return []
