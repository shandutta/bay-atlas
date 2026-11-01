#!/usr/bin/env python3
"""Private Bay Atlas HTTP service; Python standard library only."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from functools import lru_cache
import gzip
import importlib
import json
import mimetypes
import os
from pathlib import Path
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse, unquote
from urllib.request import Request, urlopen

from model import BAY_BOUNDS, estimate, exclude_market_quarantine, finite_number, in_bay_area, model_description, prepare_sales, source_provider, today_utc
from refresh import ImportBusyError, atomic_json
from address_suggest import AddressSuggestions, MAX_QUERY_LENGTH, REQUEST_TIMEOUT, REQUEST_INTERVAL, precise_address_matches

BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"
MAX_JSON_BYTES = 16_384
MAX_IMPORT_BYTES = 5 * 1024 * 1024
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
ALLOWED_HOSTS.update(host.strip().lower() for host in os.environ.get("HOMES_ALLOWED_HOSTS", "").split(",") if host.strip())


@lru_cache(maxsize=8)
def compressed_static(path: str, modified_ns: int, size: int) -> bytes:
    """Bounded gzip cache, invalidated by each static asset's file signature."""
    return gzip.compress(Path(path).read_bytes(), compresslevel=3, mtime=0)


def _public_counts(value, names):
    return {key: value[key] for key in names if type(value.get(key)) is int and value[key] >= 0}


def public_import_summary(value):
    """Expose aggregate import status, never the private review/audit record."""
    if not isinstance(value, dict):
        return None
    summary = {}
    if isinstance(value.get("counts"), dict):
        summary["counts"] = _public_counts(value["counts"], (
            "input", "eligible", "added", "updated", "duplicates", "stale", "conflicts", "total",
            "excluded_attached", "removed_attached", "excluded_quarantined", "removed_quarantined"))
    if value.get("refresh_mode") in ("manual_csv", "automatic_api"):
        summary["refresh_mode"] = value["refresh_mode"]
    if type(value.get("coverage_complete")) is bool:
        summary["coverage_complete"] = value["coverage_complete"]
    if isinstance(value.get("imported_at"), str):
        try:
            summary["imported_at"] = datetime.fromisoformat(value["imported_at"].replace("Z", "+00:00")).isoformat()
        except ValueError:
            pass
    return summary


def public_coverage_summary(value):
    """Keep display text/counts while withholding provider and review payloads."""
    if isinstance(value, str):
        return value[:2000]
    if not isinstance(value, dict):
        return {"label": "Bay Area", "complete": False}
    summary = {key: value[key][:2000] for key in ("label", "description", "note") if isinstance(value.get(key), str)}
    summary.update(_public_counts(value, ("counties_completed", "counties_total")))
    for key in ("complete", "county_searches_finished", "request_limit_reached"):
        if type(value.get(key)) is bool:
            summary[key] = value[key]
    for key, allowed in (("status", ("partial", "complete")), ("scope", ("east_bay", "bay_area")),
                         ("refresh_mode", ("manual_csv", "automatic_api"))):
        if value.get(key) in allowed:
            summary[key] = value[key]
    if isinstance(value.get("verified_source_gaps"), list):
        gaps = []
        for gap in value["verified_source_gaps"][:20]:
            if not isinstance(gap, dict) or not isinstance(gap.get("county"), str):
                continue
            public = {"county": gap["county"][:200], **_public_counts(gap, ("unreachable_count",))}
            if gap.get("source_coverage") in ("partial", "unknown"):
                public["source_coverage"] = gap["source_coverage"]
            gaps.append(public)
        summary["verified_source_gaps"] = gaps
    return summary


class DataStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.signature = None
        self.payload = None
        self._encoded_payload = None
        self._encoded = {}

    def serialized(self, *, gzip_ok=False) -> tuple[bytes, bool]:
        """Reuse wire bytes until the dataset or UTC eligibility date changes.

        The API's compatibility ``rows`` alias is deliberately omitted. Only
        one snapshot is cached, so refreshing cannot accumulate old datasets.
        """
        payload = self.snapshot()
        with self.lock:
            if payload is not self._encoded_payload:
                public = {key: value for key, value in payload.items() if key != "rows"}
                self._encoded = {False: json.dumps(public, ensure_ascii=False, allow_nan=False,
                                                  separators=(",", ":")).encode("utf-8")}
                self._encoded_payload = payload
            plain = self._encoded[False]
            if gzip_ok and len(plain) >= 1024:
                if True not in self._encoded:
                    self._encoded[True] = gzip.compress(plain, compresslevel=3, mtime=0)
                compressed = self._encoded[True]
                if len(compressed) < len(plain):
                    return compressed, True
            return plain, False

    def snapshot(self) -> dict:
        as_of = today_utc()
        try:
            stat = self.path.stat()
            signature = (stat.st_mtime_ns, stat.st_size, as_of.isoformat())
        except OSError:
            signature = (None, as_of.isoformat())
        with self.lock:
            if signature == self.signature and self.payload is not None:
                return self.payload
            warnings = []
            source = {}
            status = "ready"
            try:
                source = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(source, dict):
                    raise ValueError("Dataset must be an object")
                raw = source.get("sales", source.get("rows", []))
                if not isinstance(raw, list):
                    raise ValueError("Sales must be an array")
                eligible_raw, quarantined = exclude_market_quarantine(raw, source.get("market_sale_quarantine"))
                sales, diagnostics = prepare_sales(eligible_raw, as_of)
                if quarantined:
                    diagnostics["input_count"] = len(raw)
                    diagnostics["excluded"]["quarantined_market_sale"] = quarantined
            except FileNotFoundError:
                status = "awaiting_data"
                sales, diagnostics = [], {"input_count": 0, "eligible_count": 0, "excluded": {}}
                warnings.append("Dated sold-home records have not been imported yet.")
            except (OSError, ValueError, TypeError):
                source = {}
                status = "data_error"
                sales, diagnostics = [], {"input_count": 0, "eligible_count": 0, "excluded": {}}
                warnings.append("The saved sale data could not be read. Refresh or inspect the import before relying on estimates.")
            if not sales and status == "ready":
                status = "awaiting_data"
                warnings.append("No eligible sold-home records are available in the two-year window.")
            updated_at = source.get("updated_at")
            if updated_at:
                try:
                    updated = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - updated).total_seconds() > 36 * 3600:
                        warnings.append("Saved sale data was last refreshed more than 36 hours ago.")
                except ValueError:
                    warnings.append("The data refresh time is unavailable.")
            if isinstance(source.get("warnings"), list):
                warnings.extend(str(item)[:2000] for item in source["warnings"][:20])
            description = model_description(as_of)
            coverage = public_coverage_summary(source.get("coverage"))
            if isinstance(coverage, dict) and coverage.get("note"):
                warnings.append(str(coverage["note"])[:2000])
            providers = sorted({source_provider(row["source_url"]) for row in sales} - {None})
            provenance = str(source.get("source", " + ".join(providers) + " sold homes"))[:500]
            metadata = {
                "updated_at": updated_at, "source": provenance, "status": status,
                "sources": providers, "last_import": public_import_summary(source.get("last_import")),
                "message": " ".join(warnings), "coverage": coverage,
                "record_count": len(sales), "home_type": "Detached single-family homes",
                "weights": description["time_weights"], "as_of": as_of.isoformat(),
                "oldest_sale_date": description["oldest_sale_date"],
            }
            self.payload = {
                "updated_at": updated_at, "source": " + ".join(providers) or "Sale records", "sources": providers,
                "source_provenance": provenance, "status": status,
                "coverage": coverage, "sales": sales, "rows": sales, "metadata": metadata,
                "model": description, "warnings": warnings, "diagnostics": diagnostics,
            }
            self.signature = signature
            return self.payload


class Geocoder:
    """Submitted addresses reuse precise suggestions before Census; no batch lookup."""
    MAX_CACHE_ENTRIES = 512
    def __init__(self, cache_path: Path, *, suggestions=None):
        self.cache_path = cache_path
        self.suggestions = suggestions
        self.lock = threading.Lock()
        self.last_request = 0.0
        try:
            self.cache = json.loads(cache_path.read_text(encoding="utf-8"))
            if not isinstance(self.cache, dict):
                self.cache = {}
            self.cache = dict(list(self.cache.items())[-self.MAX_CACHE_ENTRIES:])
        except (OSError, ValueError):
            self.cache = {}

    def search(self, query: str) -> dict:
        if not isinstance(query, str) or len(query) > 250 or any(ord(char) < 32 or ord(char) == 127 for char in query):
            raise ValueError("Enter a full street address with city and state or ZIP code")
        query = " ".join(query.split())
        if not 5 <= len(query) <= 250:
            raise ValueError("Enter a full street address with city and state or ZIP code")
        if not any(char.isdigit() for char in query):
            raise ValueError("Enter a house number and street, with city and state or ZIP code")
        key = query.casefold()
        # Do not queue an unbounded number of stale address searches behind a
        # slow provider. The client can retry and the map remains responsive.
        if not self.lock.acquire(timeout=1.05):
            raise RuntimeError("Address search is busy. Try again shortly or tap a location on the map.")
        try:
            cached = self.cache.get(key)
            valid_cache = False
            try:
                if isinstance(cached, dict) and isinstance(cached.get("results"), list):
                    # Empty searches expire quickly so a provider miss cannot
                    # conceal a valid address for the full positive-cache TTL.
                    ttl = 90 * 86400 if cached["results"] else 300
                    valid_cache = 0 <= time.time() - finite_number(cached.get("saved_at")) < ttl
                    if valid_cache and cached["results"]:
                        return self._response(cached["results"], cached=True)
            except (ValueError, TypeError, OverflowError):
                pass
            suggestion_status = 'not_needed'
            if self.suggestions is not None and 6 <= len(query) <= MAX_QUERY_LENGTH:
                # Reuse the autocomplete result, including a request already in
                # flight when Enter was pressed. Only verified numbered street
                # matches can resolve a home; approximate street points cannot.
                suggested = self.suggestions.search(query, wait_timeout=REQUEST_TIMEOUT + REQUEST_INTERVAL,
                                                    precise_only=True)
                suggestion_status = suggested['remote_status']
                precise = precise_address_matches(query, suggested['results'])
                if precise:
                    providers = ' + '.join(dict.fromkeys(row['source'] for row in precise))
                    return self._response(precise, cached=suggested['cached'], provider=providers)
            if valid_cache and suggestion_status not in {'unavailable', 'busy'}:
                return self._response(cached['results'], cached=True)
            self.cache.pop(key, None)
            # Globally serialize outbound requests, allowing at most one each second.
            wait = 1.05 - (time.monotonic() - self.last_request)
            if wait > 0:
                time.sleep(wait)
            params = urlencode({"address": query, "benchmark": "Public_AR_Current", "format": "json"})
            endpoint = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress?" + params
            request = Request(endpoint, headers={"User-Agent": "BayAtlasPrivateHomeMap/1.0", "Accept": "application/json"})
            self.last_request = time.monotonic()
            try:
                with urlopen(request, timeout=15) as response:
                    body = response.read(1_000_001)
                if len(body) > 1_000_000:
                    raise ValueError("Geocoder response too large")
                payload = json.loads(body)
                if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
                    raise ValueError("Invalid geocoder response")
                matches = payload["result"].get("addressMatches")
                if not isinstance(matches, list):
                    raise ValueError("Invalid geocoder matches")
            except (URLError, HTTPError, TimeoutError, OSError, ValueError) as exc:
                raise RuntimeError("Address search is temporarily unavailable. Try again shortly or tap a location on the map.") from exc
            results = []
            for match in matches[:5]:
                try:
                    lat, lng = finite_number(match["coordinates"]["y"]), finite_number(match["coordinates"]["x"])
                    if in_bay_area(lat, lng):
                        results.append({"lat": lat, "lng": lng, "display_name": str(match["matchedAddress"]),
                                        "source": "U.S. Census Geocoder", "precision": "Street address range interpolation"})
                except (KeyError, TypeError, ValueError):
                    continue
            if not results and suggestion_status in {'unavailable', 'busy'}:
                # A Census miss is inconclusive when the other address source
                # could not be checked. Do not cache an outage as "no match".
                raise RuntimeError("Address search is temporarily unavailable. Try again shortly, add a city or ZIP code, or tap a location on the map.")
            self.cache[key] = {"saved_at": time.time(), "results": results}
            while len(self.cache) > self.MAX_CACHE_ENTRIES:
                self.cache.pop(next(iter(self.cache)))
            # Cache is operational state, outside the publicly served directory.
            try:
                atomic_json(self.cache_path, self.cache, private=True)
            except OSError:
                pass  # A read-only filesystem must not turn a successful search into an error.
            return self._response(results, cached=False)
        finally:
            self.lock.release()

    @staticmethod
    def _response(results, *, cached, provider="U.S. Census Geocoder"):
        return {"results": results, "cached": cached, "provider": provider,
                "status": "matched" if results else "no_match",
                "message": "" if results else "No matching Bay Area street address was found. Add a city or ZIP code, or tap the location on the map."}


class UnconfiguredRefresh:
    def status(self):
        return {"status": "unconfigured", "running": False, "configured": False,
                "message": "A Zillow data provider has not been configured yet."}

    def start(self):
        return self.status()


def create_refresh_manager():
    try:
        module = importlib.import_module("refresh")
    except ModuleNotFoundError as exc:
        if exc.name != "refresh":
            raise
        return UnconfiguredRefresh()
    return module.RefreshManager(BASE_DIR)


class AtlasServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, *, data_path=None, refresh_manager=None):
        super().__init__(address, AtlasHandler)
        self.store = DataStore(data_path or BASE_DIR / "data" / "sales.json")
        self.suggestions = AddressSuggestions(self.store.snapshot)
        self.geocoder = Geocoder(BASE_DIR / "data" / "geocode-cache.json", suggestions=self.suggestions)
        self.refresh_manager = refresh_manager or create_refresh_manager()


class AtlasHandler(BaseHTTPRequestHandler):
    server_version = "BayAtlas/1.0"
    timeout = 15

    def log_message(self, format, *args):
        # Do not write searched home addresses/query strings to the access log.
        path = urlparse(self.path).path
        print(f"{self.log_date_time_string()} {self.command} {path}", flush=True)

    def _path(self):
        path = unquote(urlparse(self.path).path)
        if path == "/homes":
            return "/"
        if path.startswith("/homes/"):
            return path[len("/homes"):]
        return path

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

    def _accepts_gzip(self):
        encodings = {}
        for item in self.headers.get("Accept-Encoding", "").split(","):
            parts = item.strip().lower().split(";")
            if not parts[0]:
                continue
            quality = 1.0
            for parameter in parts[1:]:
                if parameter.strip().startswith("q="):
                    try:
                        quality = float(parameter.strip()[2:])
                    except ValueError:
                        quality = 0.0
            encodings[parts[0]] = quality
        return encodings.get("gzip", encodings.get("*", 0)) > 0

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        compressed = False
        if len(body) >= 1024 and self._accepts_gzip():
            candidate = gzip.compress(body, compresslevel=3, mtime=0)
            if len(candidate) < len(body):
                body, compressed = candidate, True
        self._json_bytes(body, compressed, status)

    def _json_bytes(self, body, compressed=False, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Vary", "Accept-Encoding")
        if compressed:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _check_mutation_origin(self):
        origin = urlparse(self.headers.get("Origin", ""))
        hosts = {self.headers.get("Host", "").lower()}
        # This service binds loopback; an authenticated local reverse proxy may preserve a forwarded host.
        if self.client_address[0] in ("127.0.0.1", "::1"):
            forwarded = self.headers.get("X-Forwarded-Host", "").split(",")[0].strip().lower()
            if forwarded:
                hosts.add(forwarded)
        if origin.scheme not in ("http", "https") or origin.netloc.lower() not in hosts:
            self._json({"error": "Refresh requests must come from this site's own page."}, 403)
            return False
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            self._json({"error": "Cross-site refresh requests are not allowed."}, 403)
            return False
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            self._json({"error": "Expected application/json."}, 415)
            return False
        return True

    def _check_host(self):
        # Explicit names prevent a hostile domain rebound to loopback from becoming a trusted origin.
        if len(self.headers.get_all("Host", [])) != 1:
            self._json({"error": "Exactly one Host header is required."}, 403)
            return False
        candidates = [self.headers.get("Host", "")]
        if self.client_address[0] in ("127.0.0.1", "::1") and self.headers.get("X-Forwarded-Host"):
            candidates.append(self.headers["X-Forwarded-Host"].split(",")[0].strip())
        for candidate in candidates:
            try:
                parsed = urlparse("//" + candidate)
                valid = (parsed.hostname in ALLOWED_HOSTS and (parsed.port is None or 1 <= parsed.port <= 65535)
                         and parsed.username is None and parsed.password is None
                         and not parsed.path and not parsed.query and not parsed.fragment
                         and not any(char.isspace() for char in candidate))
            except ValueError:
                valid = False
            if not valid:
                self._json({"error": "This hostname is not allowed for the private map."}, 403)
                return False
        return True

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._check_host():
            return
        if urlparse(self.path).path == "/homes":
            self.send_response(308)
            self.send_header("Location", "/homes/")
            self.send_header("Content-Length", "0")
            self._security_headers()
            self.end_headers()
            return
        path = self._path()
        query = parse_qs(urlparse(self.path).query)
        try:
            if path == "/api/health":
                self._json({"status": "ok", "service": "Bay Atlas"})
            elif path == "/api/data":
                self._json_bytes(*self.server.store.serialized(gzip_ok=self._accepts_gzip()))
            elif path == "/api/estimate":
                values = {key: query.get(key, [default])[0] for key, default in (("lat", None), ("lng", None), ("sqft", "2500"), ("beds", "3"))}
                if values["lat"] is None or values["lng"] is None:
                    raise ValueError("Latitude and longitude are required")
                snapshot = self.server.store.snapshot()
                result = estimate(snapshot["sales"], **values, prepared=True)
                result.update(data_updated_at=snapshot["updated_at"], coverage=snapshot["coverage"], data_warnings=snapshot["warnings"])
                self._json(result)
            elif path == "/api/geocode":
                self._json(self.server.geocoder.search(query.get("q", [""])[0]))
            elif path == "/api/suggest":
                self._json(self.server.suggestions.search(query.get("q", [""])[0]))
            elif path == "/api/refresh":
                self._json(self.server.refresh_manager.status())
            elif path == "/api/provider":
                manager = self.server.refresh_manager
                self._json(manager.provider_status() if hasattr(manager, "provider_status") else manager.status())
            elif path.startswith("/api/"):
                self._json({"error": "Endpoint not found"}, 404)
            else:
                self._static(path)
        except (TypeError, ValueError) as exc:
            self._json({"error": str(exc)}, 400)
        except RuntimeError as exc:
            self._json({"error": str(exc)}, 503)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if not self._check_host():
            return
        path = self._path()
        if path not in ("/api/refresh", "/api/provider", "/api/import/redfin"):
            self._json({"error": "Endpoint not found"}, 404)
            return
        if not self._check_mutation_origin():
            return
        try:
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) > 1:
                raise ValueError("Use one Content-Length header; chunked uploads are not supported.")
            length = int(self.headers.get("Content-Length", "0"))
            limit = MAX_IMPORT_BYTES if path == "/api/import/redfin" else MAX_JSON_BYTES
            if not 0 <= length <= limit:
                self._json({"error": "Request is too large"}, 413)
                return
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("The request body was incomplete.")
            payload = json.loads(body or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            if path == "/api/import/redfin":
                if not hasattr(self.server.refresh_manager, "import_redfin"):
                    self._json({"error": "CSV import is not available on this server."}, 503)
                    return
                self._json(self.server.refresh_manager.import_redfin(payload, data_path=self.server.store.path))
            elif path == "/api/provider":
                if not hasattr(self.server.refresh_manager, "configure"):
                    self._json({"error": "Provider settings are not available yet."}, 503)
                    return
                self._json(self.server.refresh_manager.configure(payload))
            else:
                self._json(self.server.refresh_manager.start(), 202)
        except (ValueError, TypeError) as exc:
            self._json({"error": str(exc)}, 400)
        except ImportBusyError as exc:
            self._json({"error": str(exc)}, 409)
        except RuntimeError as exc:
            self._json({"error": str(exc)}, 503)
        except TimeoutError:
            self._json({"error": "The upload timed out. Try again."}, 408)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _static(self, path):
        relative = path.lstrip("/") or "index.html"
        target = (PUBLIC_DIR / relative).resolve()
        root = PUBLIC_DIR.resolve()
        if (root not in target.parents or not target.is_file()
                or any(part.startswith(".") for part in target.relative_to(root).parts)):
            self._json({"error": "Not found"}, 404)
            return
        stat = target.stat()
        etag = f'W/"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
        if etag in [value.strip() for value in self.headers.get("If-None-Match", "").split(",")]:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Cache-Control", "no-cache")
            self._security_headers()
            self.end_headers()
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        compressed = stat.st_size >= 1024 and self._accepts_gzip() and (
            content_type.startswith("text/") or content_type.startswith(("application/javascript", "application/json")))
        body = compressed_static(str(target), stat.st_mtime_ns, stat.st_size) if compressed else target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Vary", "Accept-Encoding")
        if compressed:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description="Serve the private Bay Atlas map")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback by default; expose using an authenticated reverse proxy")
    parser.add_argument("--port", type=int, default=int(os.environ.get("HOMES_PORT", "8788")))
    parser.add_argument("--data", type=Path, default=BASE_DIR / "data" / "sales.json")
    args = parser.parse_args()
    server = AtlasServer((args.host, args.port), data_path=args.data)
    print(f"Bay Atlas listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
