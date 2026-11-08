"""Built-in provider plugin: DaddyLive (linear channel directory + player extraction)."""

import asyncio
import os
import re
import urllib.parse
from typing import List, Optional, Set

import httpx
from playwright.async_api import Browser

from config import _log_failure, _upstream_media_headers, LOGGER
import provider_telemetry as telemetry
import stream_language
from stream_extractor import DEFAULT_USER_AGENT, make_soup, playwright_page
from sports_matcher import clean_sports_text, get_team_search_terms
from plugins.sdk import HtmlAggregatorProvider, StreamDict

from scrapers import (
    _channel_term_matches,
    _is_non_english_channel,
    _is_playlist_request_url,
    _verify_provider_streams,
    MAX_PROVIDER_EVENTS,
)


# How long, after nudging the player, to wait for its playlist response.
PLAYLIST_WAIT_SECONDS = 15.0


def _is_player_playlist_response(resource_type: str, status: int, url: str) -> bool:
    """The player's own playlist fetch. The page also requests the playlist URL
    as an image (a beacon/decoy answered 204 or 403) before the player's
    tokenised XHR; capturing that one gives a URL that never verifies."""
    return resource_type != "image" and status == 200 and _is_playlist_request_url(url)


def _playlist_origin(origin_header: str, referer: str) -> str:
    """The Origin to replay the playlist with: the one the player sent, else
    the Referer's origin (the playlist host rejects requests without it)."""
    if origin_header:
        return origin_header
    parsed = urllib.parse.urlsplit(referer or "")
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


# The player's playlist URLs: https://<edge-host>/<channel-slug>/index.m3u8?t=<token>.
_PLAYLIST_PATH_RE = re.compile(r"^/(premium\d+)/[^/]+\.m3u8$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{8,256}$")


def _playlist_token_request(playlist_url: str) -> Optional[tuple[str, dict]]:
    """The player's token API call for a captured playlist URL: (url, params),
    or None when the URL isn't a DaddyLive player playlist."""
    parts = urllib.parse.urlsplit(playlist_url or "")
    match = _PLAYLIST_PATH_RE.match(parts.path)
    if parts.scheme != "https" or not parts.netloc or not match:
        return None
    return f"https://{parts.netloc}/api/playlist-token", {"slug": match.group(1)}


def _with_playlist_token(playlist_url: str, token: str) -> str:
    """The playlist URL with its `t` token replaced (and the player's
    cache-buster dropped)."""
    parts = urllib.parse.urlsplit(playlist_url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k not in ("t", "_")]
    query.insert(0, ("t", token))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


class DaddyLiveScraper(HtmlAggregatorProvider):
    # Stop trying further channel pages once this many in a row produced
    # stream candidates that verification rejected (issue #49).
    MAX_REJECTED_PLAYERS_IN_A_ROW = 2

    def __init__(self):
        super().__init__("DaddyLive", os.getenv("AGGREGATOR_6_URL", "https://dlhd.pk"), ["/24-7-channels.php"], ["watch.php"])

    async def _fetch_directory_page(self, client: httpx.AsyncClient, page_url: str, browser: Optional[Browser]) -> Optional[str]:
        """DaddyLive's directory never reaches Playwright's "networkidle"
        state, so this waits a fixed 2s after navigation instead (via the
        shared `_fetch_html` HTTP->Playwright fallback)."""
        return await self._fetch_html(client, page_url, browser, settle_seconds=2, goto_timeout=25000)

    def _parse_channel_matches_from_html(
        self,
        page_html: str,
        page_url: str,
        search_terms: List[str],
        matches: List[tuple[str, int, str]],
        seen_matches: Set[str],
    ) -> None:
        """Pure CPU work (BeautifulSoup parse + per-card channel-term matching),
        split out so it can run via cpu_pool.run_cpu instead of blocking the
        shared event loop. Mutates `matches`/`seen_matches` in place to preserve
        the original cross-page, cumulative MAX_PROVIDER_EVENTS cutoff."""
        soup = make_soup(page_html)
        listed: Set[str] = set()
        try:
            self._parse_channel_anchors(soup, page_url, search_terms, matches, seen_matches, listed)
        finally:
            telemetry.report_index_events(len(listed))

    def _parse_channel_anchors(self, soup, page_url, search_terms, matches, seen_matches, listed: Set[str]) -> None:
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href") or "")
            if not self._is_event_link(href, page_url):
                continue
            match_url = urllib.parse.urljoin(page_url, href)
            listed.add(match_url)
            if match_url in seen_matches:
                continue
            title = str(anchor.get("title") or anchor.get("data-title") or "")
            raw_text = anchor.get_text(" ", strip=True)
            card_text = " ".join(part for part in (raw_text, title, str(anchor.get("aria-label") or "")) if part)
            if not stream_language.listing_allowed(card_text, _is_non_english_channel):
                continue
            matched = _channel_term_matches(card_text, search_terms) or _channel_term_matches(href, search_terms)
            score = max(
                (len(str(term).split()) * 10 + len(str(term)) for term in search_terms if _channel_term_matches(card_text, [term])),
                default=0,
            )
            if matched:
                seen_matches.add(match_url)
                matches.append((match_url, score, raw_text or title or match_url))
                if len(matches) >= MAX_PROVIDER_EVENTS:
                    return

    async def _find_matches(self, client: httpx.AsyncClient, search_terms: List[str], browser: Optional[Browser] = None) -> List[tuple[str, int, str]]:
        """Match individual directory cards without inheriting the grid's text."""
        return await self._find_matches_using(
            client,
            search_terms,
            browser,
            fetch_page=self._fetch_directory_page,
            parse_matches=self._parse_channel_matches_from_html,
            catch_page_errors=False,
        )

    async def _extract_player_streams(self, browser: Browser, watch_url: str, score: int, match_title: str) -> List[dict]:
        """Follow the embedded player iframe chain and capture the real HLS manifest.

        DaddyLive watch pages embed the actual player one or two iframes deep
        (watch.php -> dlive.sx/stream/stream-<id>.php -> <player-host>). Listening
        for playlist requests across every frame in the page reliably surfaces the
        genuine manifest, which is requested from the player iframe's origin.
        """
        streams: List[dict] = []
        if not browser or not browser.is_connected():
            return streams

        try:
            async with playwright_page(browser, user_agent=DEFAULT_USER_AGENT) as page:
                pending: List[asyncio.Future] = []

                async def record(response) -> None:
                    request = response.request
                    url = response.url
                    if any(s["url"] == url for s in streams):
                        return
                    try:
                        frame_url = request.frame.url if request.frame else ""
                    except Exception:
                        frame_url = ""
                    try:
                        # request.headers leaves out Origin; all_headers() has it.
                        headers = await request.all_headers()
                    except Exception:
                        headers = dict(request.headers)
                    referer = headers.get("referer") or frame_url or watch_url
                    streams.append({
                        "url": url,
                        "provider": self.name,
                        "match_score": score,
                        "match_title": match_title,
                        "referer": referer,
                        "origin": _playlist_origin(headers.get("origin", ""), referer),
                        "discovery_method": "playwright",
                    })

                def on_response(response) -> None:
                    try:
                        resource_type = response.request.resource_type
                    except Exception:
                        resource_type = ""
                    if _is_player_playlist_response(resource_type, response.status, response.url):
                        pending.append(asyncio.ensure_future(record(response)))

                page.on("response", on_response)
                try:
                    await page.goto(watch_url, wait_until="domcontentloaded", timeout=30000)
                except Exception as exc:
                    _log_failure("DaddyLive watch page load", exc)

                # Let nested iframes attach, then nudge the player to start.
                await asyncio.sleep(5)
                for frame in list(page.frames):
                    try:
                        await frame.mouse.click(400, 300)
                    except Exception:
                        pass
                try:
                    await page.mouse.click(400, 300)
                except Exception:
                    pass
                # The player fetches a playlist token before its playlist, so
                # wait until one has been captured rather than a fixed time.
                loop = asyncio.get_running_loop()
                deadline = loop.time() + PLAYLIST_WAIT_SECONDS
                while not streams and loop.time() < deadline:
                    await asyncio.sleep(0.5)
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
        except Exception as exc:
            _log_failure("DaddyLive player extraction", exc)

        seen = set()
        return [s for s in streams if s["url"] not in seen and not seen.add(s["url"])]

    async def refresh_stream(self, stream: StreamDict, *, http_client: httpx.AsyncClient) -> Optional[StreamDict]:
        """The player's playlist token lasts about a minute, and the player
        renews it from the edge host's token API; do the same, rather than
        driving a browser through the whole watch page again."""
        url = str(stream.get("url") or "")
        request = _playlist_token_request(url)
        if request is None:
            return None
        token_url, params = request
        headers = _upstream_media_headers(str(stream.get("referer") or ""), str(stream.get("origin") or ""))
        try:
            response = await http_client.get(token_url, params=params, headers=headers, timeout=8.0)
            token = response.json().get("token") if response.status_code == 200 else None
        except Exception as exc:
            _log_failure("DaddyLive playlist token refresh", exc)
            return None
        if not isinstance(token, str) or not _TOKEN_RE.match(token):
            return None
        return {"url": _with_playlist_token(url, token)}

    async def search(self, query_or_terms, browser: Optional[Browser] = None, http_client: Optional[httpx.AsyncClient] = None) -> List[dict]:
        if not browser or not browser.is_connected():
            return []

        search_terms = get_team_search_terms(query_or_terms, query_or_terms) if isinstance(query_or_terms, str) else list(query_or_terms or [])
        owns_client = http_client is None
        client = http_client or httpx.AsyncClient(follow_redirects=True, timeout=12.0)

        try:
            matches = await self._find_matches(client, search_terms, browser=browser)
            if not matches:
                return []

            def match_specificity(event: tuple[str, int, str]) -> tuple[int, int, int]:
                _, score, title = event
                clean_title = clean_sports_text(title)
                exact = 0
                for term in search_terms:
                    clean_term = clean_sports_text(term)
                    if clean_term and re.search(rf"\b{re.escape(clean_term)}\b", clean_title):
                        exact = max(exact, len(clean_term.split()) * 10 + len(clean_term))
                # Strongly prioritize US English feeds (e.g. "USA" in card title)
                us_priority = 100 if re.search(r"\b(?:usa|us)\b", title, re.IGNORECASE) else 0
                return us_priority, exact, score

            specific_matches = [match for match in matches if match_specificity(match)[1] > 0 or match_specificity(match)[0] > 0]
            matches = sorted(specific_matches or matches, key=match_specificity, reverse=True)

            verified_streams: List[dict] = []
            rejected_in_a_row = 0
            candidates = matches[:8]
            for position, (match_url, score, match_title) in enumerate(candidates):
                streams = await self._extract_player_streams(browser, match_url, score, match_title)
                verified = await _verify_provider_streams(streams, client)
                if verified:
                    verified_streams.extend(verified)
                    rejected_in_a_row = 0
                elif streams:
                    rejected_in_a_row += 1
                    if rejected_in_a_row >= self.MAX_REJECTED_PLAYERS_IN_A_ROW:
                        # Every manifest the players requested failed
                        # verification. The remaining, less specific channels
                        # use the same CDN: stop instead of spending ~10 s of
                        # browser time on each.
                        LOGGER.info(
                            "provider %s: stream candidates of %d channel page(s) in a row were all rejected; "
                            "skipping the remaining %d match(es)",
                            self.name, rejected_in_a_row, len(candidates) - position - 1,
                        )
                        break

            seen_urls: Set[str] = set()
            return [
                stream for stream in verified_streams
                if stream.get("url") and not (stream["url"] in seen_urls or seen_urls.add(stream["url"]))
            ]
        finally:
            if owns_client:
                await client.aclose()
