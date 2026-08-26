from __future__ import annotations

import json
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .models import Book, Market


class APIError(RuntimeError):
    pass


class JSONClient:
    def __init__(self, timeout: float = 20.0, retries: int = 4) -> None:
        self.timeout = timeout
        self.retries = retries
        self.ssl_context = ssl.create_default_context()
        verify = ssl.get_default_verify_paths()
        # Framework Python on macOS can have no installed OpenSSL CA file even
        # though the OS bundle exists. Load that bundle while retaining full TLS
        # hostname and certificate verification.
        if verify.cafile is None:
            for candidate in (Path("/etc/ssl/cert.pem"), Path("/private/etc/ssl/cert.pem")):
                if candidate.is_file():
                    self.ssl_context.load_verify_locations(cafile=str(candidate))
                    break

    def request(self, method: str, url: str, payload: Any | None = None) -> Any:
        body = None if payload is None else json.dumps(payload).encode()
        headers = {"Accept": "application/json", "User-Agent": "polyalpha-lab/0.1"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(url, data=body, method=method, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl_context) as response:
                    return json.loads(response.read())
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if isinstance(exc, urllib.error.HTTPError) and exc.code < 500 and exc.code != 429:
                    break
                time.sleep(min(8.0, (2**attempt) + random.random() * 0.2))
        raise APIError(f"{method} {url} failed: {last_error}")


class PolymarketClient:
    """Public, read-only Polymarket client. It contains no trading endpoint."""

    gamma_url = "https://gamma-api.polymarket.com"
    clob_url = "https://clob.polymarket.com"

    def __init__(self, http: JSONClient | None = None) -> None:
        self.http = http or JSONClient()

    def market_pages(self, limit: int = 100) -> Iterator[list[Market]]:
        cursor: str | None = None
        while True:
            query: dict[str, str | int] = {
                "limit": min(100, max(1, limit)),
                "closed": "false",
                "include_tag": "true",
            }
            if cursor:
                query["after_cursor"] = cursor
            url = f"{self.gamma_url}/markets/keyset?{urllib.parse.urlencode(query)}"
            raw = self.http.request("GET", url)
            rows = raw.get("markets", []) if isinstance(raw, dict) else []
            markets = [Market.from_gamma(item) for item in rows if isinstance(item, dict)]
            yield markets
            cursor = raw.get("next_cursor") if isinstance(raw, dict) else None
            if not cursor or len(rows) < query["limit"]:
                return

    def active_event_market_pages(self, limit: int = 500) -> Iterator[list[Market]]:
        """Fetch active events and flatten their nested markets.

        Starting from open events avoids walking the historical market catalog.
        Keyset pagination also avoids Gamma's finite offset window.
        """
        cursor: str | None = None
        while True:
            query = {
                "limit": min(500, max(1, limit)),
                "closed": "false",
            }
            if cursor:
                query["after_cursor"] = cursor
            url = f"{self.gamma_url}/events/keyset?{urllib.parse.urlencode(query)}"
            raw = self.http.request("GET", url)
            events = raw.get("events", []) if isinstance(raw, dict) else []
            page: list[Market] = []
            for event in events:
                if not isinstance(event, dict) or not bool(event.get("active", True)) or bool(event.get("archived", False)):
                    continue
                event_context = {
                    "id": event.get("id"),
                    "category": event.get("category"),
                    "negRisk": event.get("negRisk", False),
                }
                for item in event.get("markets") or []:
                    if isinstance(item, dict):
                        enriched = dict(item)
                        enriched["events"] = [event_context]
                        page.append(Market.from_gamma(enriched))
            yield page
            cursor = raw.get("next_cursor") if isinstance(raw, dict) else None
            if not cursor or len(events) < query["limit"]:
                return

    def all_tradable_markets(self, max_markets: int | None = None) -> list[Market]:
        result: list[Market] = []
        seen: set[str] = set()
        for page in self.active_event_market_pages():
            for market in page:
                if (
                    market.id not in seen
                    and market.active
                    and not market.closed
                    and market.accepting_orders
                    and market.binary_tokens() is not None
                ):
                    result.append(market)
                    seen.add(market.id)
                    if max_markets is not None and len(result) >= max_markets:
                        return result
        return result

    def get_market(self, market_id: str) -> Market:
        encoded = urllib.parse.quote(market_id, safe="")
        raw = self.http.request("GET", f"{self.gamma_url}/markets/{encoded}")
        if not isinstance(raw, dict):
            raise APIError(f"Invalid market payload for {market_id}")
        return Market.from_gamma(raw)

    def books(self, token_ids: list[str], batch_size: int = 100) -> dict[str, Book]:
        result: dict[str, Book] = {}
        for start in range(0, len(token_ids), batch_size):
            chunk = token_ids[start : start + batch_size]
            payload = [{"token_id": token_id} for token_id in chunk]
            raw = self.http.request("POST", f"{self.clob_url}/books", payload)
            rows = raw if isinstance(raw, list) else raw.get("data", [])
            for item in rows:
                if isinstance(item, dict):
                    book = Book.from_api(item)
                    if book.asset_id:
                        result[book.asset_id] = book
        return result
