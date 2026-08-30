"""Resolve iyf.tv links to the equivalent iyf.lv show id."""

import hashlib
import json
import re
from dataclasses import dataclass

import httpx

from . import engine

IYFTV_CONFIG_PAGE = "https://www.iyf.tv/"
IYFTV_DETAIL_API = "https://m10.iyf.tv/v3/video/detail"
_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0 Safari/537.36"
)
_RE_PCONFIG = re.compile(
    r'"pConfig"\s*:\s*\{\s*"publicKey"\s*:\s*"([^"]+)"\s*,\s*'
    r'"privateKey"\s*:\s*\[(.*?)\]\s*\}',
    re.S,
)
_RE_KEY_Q = re.compile(r"[?&]v=([A-Za-z0-9]{4,32})")
_RE_KEY_PATH = re.compile(r"/(?:play|detail|video)/([A-Za-z0-9]{4,32})")
_RE_EP_PATH = re.compile(r"[-/](\d+)-(\d+)/?(?:$|[?&#])")
_RE_EP_Q = re.compile(r"[?&](?:e|ep|episode)=(\d+)")
_RE_ARABIC_SEASON = re.compile(r"第(\d+)季")
_RE_CN_SEASON = re.compile(r"第([一二三四五六七八九十百]+)季")
_RE_PATH_ID = re.compile(r"/(\d{3,})")


@dataclass
class SigningConfig:
    """Rotating iyf.tv API signing keys."""

    public_key: str
    private_key: str


@dataclass
class TitleSeason:
    """A title with its extracted season number."""

    base: str
    season: int


def _http_get(url: str, timeout: float = 30.0) -> str:
    response = httpx.get(
        url,
        headers={"User-Agent": _DESKTOP_UA, "Referer": IYFTV_CONFIG_PAGE},
        timeout=timeout,
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.text


def _json_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _get_pconfig() -> SigningConfig:
    match = _RE_PCONFIG.search(_http_get(IYFTV_CONFIG_PAGE))
    if not match:
        raise engine.IyfError("could not find the iyf.tv signing pConfig")
    private_keys = re.findall(r'"([^"]+)"', match.group(2))
    if not private_keys:
        raise engine.IyfError("iyf.tv signing pConfig has no private key")
    return SigningConfig(match.group(1), private_keys[0])


def _iyftv_title(key: str) -> str:
    query = (
        "ispath=false&cinema=1&device=1&player=CkPlayer&tech=HLS"
        f"&country=HU&lang=cns&v=1&id={key}&region=GL"
    )
    config = _get_pconfig()
    signature = hashlib.md5(
        f"{config.public_key}&{query.lower()}&{config.private_key}".encode()
    ).hexdigest()
    data = _json_object(
        json.loads(
            _http_get(
                f"{IYFTV_DETAIL_API}?{query}&vv={signature}&pub={config.public_key}"
            )
        )
    )
    detail = _json_object(data.get("data"))
    info = detail.get("info")
    first_info = _json_object(info[0]) if isinstance(info, list) and info else {}
    title = first_info.get("title")
    if not isinstance(title, str) or not title:
        raise engine.IyfError(f"iyf.tv key {key!r} has no title")
    return title


def _split_title_season(title: str) -> TitleSeason:
    match = _RE_ARABIC_SEASON.search(title) or _RE_CN_SEASON.search(title)
    if not match:
        return TitleSeason(title.strip(), 1)
    season = match.group(1)
    number = int(season) if season.isdigit() else engine.parse_chinese_numeral(season)
    return TitleSeason(title[: match.start()].strip(), number or 1)


def _pick_best(results: list[engine.SearchResult], season: int) -> str:
    for result in results:
        if engine.season_from_title(result.title) == season:
            return result.show_id
    return results[0].show_id


def _iyftv_to_iyflv(key: str, episode: int | None) -> engine.Source:
    title = _iyftv_title(key)
    title_season = _split_title_season(title)
    query = f"{title_season.base} 第{title_season.season}季"
    results = engine.search(query) or engine.search(title_season.base)
    if not results:
        raise engine.IyfError(f"iyf.tv show {title!r} not found on iyf.lv")
    return engine.Source(_pick_best(results, title_season.season), episode=episode)


def _extract_key(url: str) -> str | None:
    match = _RE_KEY_Q.search(url) or _RE_KEY_PATH.search(url)
    return match.group(1) if match else None


def _extract_episode(url: str) -> int | None:
    query_match = _RE_EP_Q.search(url)
    if query_match:
        return int(query_match.group(1))
    path_match = _RE_EP_PATH.search(url)
    return int(path_match.group(2)) if path_match else None


def normalize_url(url: str) -> engine.Source:
    """Return a normalized source for an iyf.tv URL."""
    key = _extract_key(url)
    if key:
        return _iyftv_to_iyflv(key, _extract_episode(url))
    match = _RE_PATH_ID.search(url)
    if match:
        return engine.Source(match.group(1), episode=_extract_episode(url))
    raise engine.IyfError(f"no iyf.tv video key in {url!r}")
