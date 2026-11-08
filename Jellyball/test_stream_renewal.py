"""DaddyLive player capture and in-place playlist token renewal
(Provider.refresh_stream + failover's renew-before-rescrape path)."""

import asyncio
import json
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

import failover
import scrapers  # noqa: F401  (loads the plugins before the provider import)
import sessions
import state
from plugins.builtin.daddylive.provider import (
    DaddyLiveScraper,
    _is_player_playlist_response,
    _playlist_origin,
    _playlist_token_request,
    _with_playlist_token,
)
from network_safety import clear_dns_cache
from plugins.sdk import Provider
from test_failover_recovery import StateMixin, _candidate
from test_fixtures_contract import _public_getaddrinfo

PLAYLIST = "https://edge.example.test/premium44/index.m3u8?t=1700000000.oldtoken&_=1700000000000"


class PlayerCaptureTests(unittest.TestCase):
    def test_only_the_players_own_playlist_fetch_is_captured(self):
        url = "https://edge.example.test/premium44/index.m3u8?t=abc"
        self.assertTrue(_is_player_playlist_response("fetch", 200, url))
        self.assertTrue(_is_player_playlist_response("xhr", 200, url))
        # The page's image-type request for the same URL is a decoy.
        self.assertFalse(_is_player_playlist_response("image", 200, url))
        self.assertFalse(_is_player_playlist_response("fetch", 403, url))
        self.assertFalse(_is_player_playlist_response("fetch", 200, "https://edge.example.test/api/playlist-token"))

    def test_origin_falls_back_to_the_referers_origin(self):
        self.assertEqual(_playlist_origin("https://player.example.test", "https://other.example.test/"), "https://player.example.test")
        self.assertEqual(_playlist_origin("", "https://player.example.test/premiumtv/x.php?id=44"), "https://player.example.test")
        self.assertEqual(_playlist_origin("", ""), "")
        self.assertEqual(_playlist_origin("", "about:blank"), "")


class TokenUrlTests(unittest.TestCase):
    def test_token_request_for_a_player_playlist(self):
        self.assertEqual(
            _playlist_token_request(PLAYLIST),
            ("https://edge.example.test/api/playlist-token", {"slug": "premium44"}),
        )

    def test_no_token_request_for_other_urls(self):
        for url in (
            "http://edge.example.test/premium44/index.m3u8",
            "https://edge.example.test/hls/espn/index.m3u8",
            "https://edge.example.test/premium44/sub/index.m3u8",
            "",
        ):
            self.assertIsNone(_playlist_token_request(url), url)

    def test_token_is_replaced_and_cache_buster_dropped(self):
        renewed = _with_playlist_token(PLAYLIST + "&q=1", "1700000100.newtoken")
        self.assertEqual(renewed, "https://edge.example.test/premium44/index.m3u8?t=1700000100.newtoken&q=1")
        self.assertEqual(
            sessions.candidate_source_key({"provider": "DaddyLive", "url": renewed}),
            sessions.candidate_source_key({"provider": "DaddyLive", "url": PLAYLIST}),
        )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class RefreshStreamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # The token API goes through safe_get: resolve hosts to a public
        # address so the SSRF check doesn't depend on network access.
        clear_dns_cache()
        patcher = patch.object(socket, "getaddrinfo", side_effect=_public_getaddrinfo)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(clear_dns_cache)

    stream = {"url": PLAYLIST, "provider": "DaddyLive", "referer": "https://player.example.test/", "origin": "https://player.example.test"}

    async def test_renews_the_token_with_the_players_headers(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, text=json.dumps({"token": "1700000100.newtoken", "ttl": 120}))

        async with _client(handler) as client:
            renewed = await DaddyLiveScraper().refresh_stream(self.stream, http_client=client)
        self.assertEqual(renewed, {"url": "https://edge.example.test/premium44/index.m3u8?t=1700000100.newtoken"})
        self.assertEqual(str(seen[0].url), "https://edge.example.test/api/playlist-token?slug=premium44")
        self.assertEqual(seen[0].headers["Origin"], "https://player.example.test")
        self.assertEqual(seen[0].headers["Referer"], "https://player.example.test/")

    async def test_nothing_on_a_refused_or_odd_token_response(self):
        responses = [
            httpx.Response(403, text="forbidden"),
            httpx.Response(200, text="<html>not json</html>"),
            httpx.Response(200, text=json.dumps({"token": "bad token with spaces"})),
            httpx.Response(200, text=json.dumps({"ttl": 120})),
            httpx.Response(200, text=json.dumps(["not", "an", "object"])),
        ]
        for response in responses:
            async with _client(lambda request, r=response: r) as client:
                self.assertIsNone(await DaddyLiveScraper().refresh_stream(self.stream, http_client=client))

    async def test_token_api_is_not_followed_onto_a_private_address(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8096/api/playlist-token"})

        # Like the shared client, which follows redirects.
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            self.assertIsNone(await DaddyLiveScraper().refresh_stream(self.stream, http_client=client))
        self.assertEqual(seen, ["https://edge.example.test/api/playlist-token?slug=premium44"])

    async def test_nothing_for_a_non_player_url(self):
        def handler(request):
            raise AssertionError("no request expected")

        async with _client(handler) as client:
            stream = dict(self.stream, url="https://edge.example.test/hls/espn/index.m3u8")
            self.assertIsNone(await DaddyLiveScraper().refresh_stream(stream, http_client=client))

    async def test_base_provider_cannot_renew(self):
        async with _client(lambda request: httpx.Response(500)) as client:
            self.assertIsNone(await Provider().refresh_stream({"url": PLAYLIST}, http_client=client))


class FailoverRenewalTests(StateMixin, unittest.IsolatedAsyncioTestCase):
    def _setup_channel(self):
        a = _candidate("DL", PLAYLIST, last_health_ok=True)
        b = _candidate("B", "https://b.example.test/live.m3u8", last_health_ok=True)
        state.stream_state["t"] = {"name": "Team", "candidates": [a, b], "active_index": 0}
        return a

    async def _forbidden(self, a, provider):
        with patch.object(failover, "_provider_named", return_value=provider), \
                patch.object(failover, "_trigger_scrape", new=AsyncMock()) as rescrape, \
                patch.object(failover.SESSIONS, "poke"):
            moved = await failover.request_failover("t", sessions.candidate_source_key(a), "playlist forbidden")
            await asyncio.sleep(0)
        return moved, rescrape

    async def test_forbidden_playlist_is_renewed_in_place(self):
        a = self._setup_channel()
        new_url = "https://edge.example.test/premium44/index.m3u8?t=fresh.token"
        provider = SimpleNamespace(refresh_stream=AsyncMock(return_value={"url": new_url}))
        moved, rescrape = await self._forbidden(a, provider)
        self.assertFalse(moved)
        self.assertEqual(state.stream_state["t"]["active_index"], 0)
        self.assertEqual(a["url"], new_url)
        self.assertTrue(a["last_health_ok"])
        rescrape.assert_not_awaited()

    async def test_falls_back_to_rescrape_when_the_provider_cannot_renew(self):
        a = self._setup_channel()
        provider = SimpleNamespace(refresh_stream=AsyncMock(return_value=None))
        moved, rescrape = await self._forbidden(a, provider)
        self.assertEqual(a["url"], PLAYLIST)
        self.assertFalse(a["last_health_ok"])
        rescrape.assert_awaited_once_with("t", force=True)

    async def test_a_renewal_that_did_not_take_is_not_repeated(self):
        a = self._setup_channel()
        provider = SimpleNamespace(refresh_stream=AsyncMock(side_effect=[
            {"url": "https://edge.example.test/premium44/index.m3u8?t=one"},
            {"url": "https://edge.example.test/premium44/index.m3u8?t=two"},
        ]))
        await self._forbidden(a, provider)
        moved, rescrape = await self._forbidden(a, provider)
        self.assertEqual(provider.refresh_stream.await_count, 1)
        rescrape.assert_awaited_once_with("t", force=True)

    async def test_a_renewal_pointing_at_another_source_is_ignored(self):
        a = self._setup_channel()
        provider = SimpleNamespace(refresh_stream=AsyncMock(return_value={"url": "https://other.example.test/premium44/index.m3u8"}))
        moved, rescrape = await self._forbidden(a, provider)
        self.assertEqual(a["url"], PLAYLIST)
        rescrape.assert_awaited_once_with("t", force=True)

    async def test_other_failure_reasons_do_not_renew(self):
        a = self._setup_channel()
        provider = SimpleNamespace(refresh_stream=AsyncMock(return_value={"url": "https://edge.example.test/premium44/index.m3u8?t=x"}))
        with patch.object(failover, "_provider_named", return_value=provider), \
                patch.object(failover, "_trigger_scrape", new=AsyncMock()):
            await failover.request_failover("t", sessions.candidate_source_key(a), "playlist stale")
        provider.refresh_stream.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
