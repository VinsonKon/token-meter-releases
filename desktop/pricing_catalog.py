"""Public OpenAI price data, refreshed independently of client releases.

Lookup never performs I/O. Only the explicitly configured background refresher
fetches the public documentation; no account data or API key is sent.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Callable
from urllib.request import Request, urlopen
import uuid

import release_config


PRICING_PAGE_URL = "https://developers.openai.com/api/docs/pricing"
PRICING_DATA_URL = PRICING_PAGE_URL + ".md"
REFRESH_SECONDS = 24 * 60 * 60
RETRY_SECONDS = 30 * 60
MAX_DOCUMENT_BYTES = 512 * 1024
SCHEMA_VERSION = 1
TIERS = frozenset(("standard", "batch", "flex", "fast", "ultrafast"))
_MODEL_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,95}")
_MONEY = re.compile(r"\$(\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class TokenRates:
    input: float
    cached_input: float | None
    output: float
    cache_write: float | None = None

    def legacy_tuple(self) -> tuple[float, float, float]:
        # A dash means no cached-input discount, not free input tokens.
        return (self.input, self.input if self.cached_input is None
                else self.cached_input, self.output)

    def to_json(self) -> dict:
        return {"input": self.input, "cached_input": self.cached_input,
                "output": self.output, "cache_write": self.cache_write}


@dataclass(frozen=True)
class PricingStatus:
    source: str = "builtin"
    fetched_at: float = 0
    model_count: int = 0
    refreshing: bool = False
    error: str = ""


def matching_model_id(value: object, model_ids) -> str | None:
    model_id = str(value or "").strip().lower()
    if model_id in model_ids:
        return model_id
    # Only dated snapshots inherit a base model's price. For example, a future
    # '-pro' or '-mini' must not silently inherit the base model's rate.
    match = re.fullmatch(r"(.+)-\d{4}-\d{2}-\d{2}", model_id)
    return match[1] if match and match[1] in model_ids else None


def _price(cell: str) -> float | None:
    cell = cell.strip()
    if cell in ("-", "—", "N/A"):
        return None
    match = _MONEY.fullmatch(cell)
    if not match:
        raise ValueError(f"Unrecognized token price: {cell!r}")
    number = float(match[1])
    if not math.isfinite(number) or number < 0 or number > 10000:
        raise ValueError("Token price outside supported range")
    return number


def parse_openai_pricing(markdown: str) -> dict:
    """Parse named token columns, keeping service tiers and context separate.

    Audio, images, per-call tools and fine-tuning tables are deliberately
    excluded: their token units cannot be inferred from the session totals.
    Missing/changed table structure is an error, never a replacement of the
    last working catalogue with an empty one.
    """
    models: dict = {}
    tier: str | None = None
    headers: list[str] | None = None
    in_finetuning = False
    recognized_standard = False
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        heading = re.sub(r"^#+\s*", "", line).lower()
        if heading in ("flagship models", "cyber models", "life sciences models",
                       "specialized models"):
            tier, headers = "standard", None
            continue
        if heading in ("multimodal models", "tools", "cloud platforms"):
            tier, headers = None, None
            continue
        if heading in ("finetuning", "fine-tuning"):
            in_finetuning = True
        if heading in TIERS:
            tier, headers = heading, None
            continue
        named_tier = re.fullmatch(r"(standard|batch|flex|fast|ultrafast) pricing data",
                                 heading)
        if named_tier:
            tier, headers = named_tier[1], None
            recognized_standard |= tier == "standard"
            continue
        if not line.startswith("|") or in_finetuning:
            headers = None
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if "Model" in cells:
            headers = [cell.lower() for cell in cells]
            # Do not misread text/image/audio mixed tables or training prices.
            if (any(name in headers for name in ("modality", "training", "use case"))
                    or "model" not in headers):
                headers = None
            continue
        if not headers or not tier or all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue
        if len(cells) != len(headers):
            raise ValueError("Pricing table column count changed")
        row = dict(zip(headers, cells))
        if "output" not in row and "short context output" not in row:
            continue
        model_cell = row["model"]
        # Official table decorations such as '(<272K context length)' are not
        # part of the API model id.
        model_id = re.sub(r"\s+\([^)]*\)$", "", model_cell).strip().lower()
        if not _MODEL_ID.fullmatch(model_id):
            continue
        bands = {}
        for band, prefix in (("short", "short context "), ("long", "long context ")):
            if band == "short" and prefix + "input" not in row:
                prefix = ""
            if prefix + "input" not in row or prefix + "output" not in row:
                continue
            output_rate = _price(row[prefix + "output"])
            if output_rate is None:
                continue
            input_rate = _price(row[prefix + "input"])
            if input_rate is None:
                continue
            rates = TokenRates(input_rate,
                               _price(row.get(prefix + "cached input", "-")),
                               output_rate,
                               _price(row.get(prefix + "cache writes", "-")))
            bands[band] = rates.to_json()
        if not bands:
            continue
        model_tiers = models.setdefault(model_id, {})
        previous = model_tiers.get(tier)
        if previous is not None and previous != bands:
            raise ValueError(f"Conflicting prices for {model_id}/{tier}")
        model_tiers[tier] = bands
    if not recognized_standard or not any("standard" in m for m in models.values()):
        raise ValueError("Official Standard pricing table is missing")
    context = re.search(r"Short context:\s*≤\s*([\d,]+)K input tokens", markdown)
    return {"models": models,
            "long_context_above_tokens": int(context[1].replace(",", "")) * 1000
            if context else None}


def _valid_rate(value, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("Invalid cached token rate")
    if not math.isfinite(value) or value < 0 or value > 10000:
        raise ValueError("Invalid cached token rate")
    return float(value)


def validate_catalogue(data: object) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        raise ValueError("Invalid pricing catalogue")
    models = data["models"]
    if not models or len(models) > 2000:
        raise ValueError("Invalid pricing model count")
    for model_id, tiers in models.items():
        if (not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id)
                or not isinstance(tiers, dict) or not tiers):
            raise ValueError("Invalid pricing model")
        for tier, bands in tiers.items():
            if tier not in TIERS or not isinstance(bands, dict) or not bands:
                raise ValueError("Invalid pricing tier")
            for band, rates in bands.items():
                if band not in ("short", "long") or not isinstance(rates, dict):
                    raise ValueError("Invalid pricing context band")
                _valid_rate(rates.get("input"))
                _valid_rate(rates.get("output"))
                _valid_rate(rates.get("cached_input"), optional=True)
                _valid_rate(rates.get("cache_write"), optional=True)
    threshold = data.get("long_context_above_tokens")
    if threshold is not None and (type(threshold) is not int or not 0 < threshold < 10_000_000):
        raise ValueError("Invalid context threshold")
    return data


class PricingCatalogue:
    def __init__(self, cache_path: Path | None = None, *, allow_live: bool = False):
        self.cache_path = cache_path
        self.allow_live = allow_live
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._catalogue: dict = {"models": {}}
        self._status = PricingStatus()
        self._last_attempt = float("-inf")
        self.callback: Callable[[PricingStatus], None] | None = None
        if cache_path:
            self._load_cache()

    def _load_cache(self) -> None:
        try:
            if self.cache_path.stat().st_size > MAX_DOCUMENT_BYTES:
                return
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if data.get("schema_version") != SCHEMA_VERSION or data.get("source") != PRICING_DATA_URL:
                return
            fetched_at = data.get("fetched_at")
            if (isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float))
                    or not math.isfinite(fetched_at) or not 0 < fetched_at <= time.time() + 300):
                return
            validate_catalogue(data)
            self._catalogue = data
            self._status = PricingStatus("cache", fetched_at, len(data["models"]))
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def status(self) -> PricingStatus:
        with self._lock:
            result = self._status
        if (result.source in ("live", "cache") and
                time.time() - result.fetched_at >= REFRESH_SECONDS):
            return replace(result, source="stale-cache")
        return result

    def lookup(self, model: object, *, tier: str = "standard",
               context_tokens: int | None = None) -> tuple[str, TokenRates] | None:
        with self._lock:
            catalogue = self._catalogue
        model_id = matching_model_id(model, catalogue["models"])
        if model_id is None:
            return None
        # Only an explicit per-request context count selects long-context rates;
        # a day's cumulative input count is not a request context length.
        threshold = catalogue.get("long_context_above_tokens")
        band = "long" if (context_tokens is not None and threshold is not None
                          and context_tokens > threshold) else "short"
        tier = {"default": "standard", "priority": "fast"}.get(tier, tier)
        rates = catalogue["models"][model_id].get(tier, {}).get(band)
        if rates is None:
            return None
        return model_id, TokenRates(**rates)

    def _write_cache(self, data: dict) -> None:
        if self.cache_path is None:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.parent / f".model-pricing-{uuid.uuid4().hex}.tmp"
        try:
            # One exclusive create attempt also fails promptly on a read-only
            # Windows directory; tempfile's permission retries can be lengthy.
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, allow_nan=False)
            os.replace(temporary, self.cache_path)
        finally:
            temporary.unlink(missing_ok=True)

    def refresh(self, *, force: bool = False) -> PricingStatus:
        if not self.allow_live or not self._refresh_lock.acquire(blocking=False):
            return self.status()
        try:
            now = time.time()
            current = self.status()
            if not force and (now - current.fetched_at < REFRESH_SECONDS or
                              now - self._last_attempt < RETRY_SECONDS):
                return current
            self._last_attempt = now
            with self._lock:
                self._status = replace(self._status, refreshing=True)
            try:
                request = Request(PRICING_DATA_URL, headers={
                    "User-Agent": f"Codex-Meter/{release_config.APP_VERSION}",
                    "Accept": "text/markdown,text/plain",
                })
                with urlopen(request, timeout=8.0) as response:
                    if response.geturl() != PRICING_DATA_URL:
                        raise ValueError("Unexpected pricing source redirect")
                    raw = response.read(MAX_DOCUMENT_BYTES + 1)
                if len(raw) > MAX_DOCUMENT_BYTES:
                    raise ValueError("Pricing document is too large")
                parsed = validate_catalogue(parse_openai_pricing(raw.decode("utf-8")))
                standard_count = sum("standard" in model for model in parsed["models"].values())
                with self._lock:
                    old_count = sum("standard" in model
                                    for model in self._catalogue["models"].values())
                if standard_count < max(5, old_count // 2):
                    raise ValueError("Incomplete Standard pricing document")
                data = {"schema_version": SCHEMA_VERSION, "source": PRICING_DATA_URL,
                        "fetched_at": time.time(), **parsed}
                cache_error = ""
                try:
                    self._write_cache(data)
                except OSError as exc:
                    cache_error = f"Local price cache could not be saved: {exc}"
                with self._lock:
                    self._catalogue = data
                    self._status = PricingStatus("live", data["fetched_at"],
                                                 len(data["models"]), error=cache_error)
            except (OSError, ValueError, TypeError) as exc:
                with self._lock:
                    self._status = replace(self._status, refreshing=False,
                                           error=str(exc) or type(exc).__name__)
            result = self.status()
            if self.callback:
                try:
                    self.callback(result)
                except Exception:
                    pass
            return result
        finally:
            self._refresh_lock.release()

    def refresh_in_background(self, *, force: bool = False) -> bool:
        now = time.time()
        current = self.status()
        if (not self.allow_live or current.refreshing or
                (not force and (now - current.fetched_at < REFRESH_SECONDS or
                                now - self._last_attempt < RETRY_SECONDS))):
            return False
        threading.Thread(target=self.refresh, kwargs={"force": force},
                         name="model-price-refresh", daemon=True).start()
        return True


# Imports and tests remain offline. sync.run explicitly configures this service.
active = PricingCatalogue()


def configure(cache_path: Path, *, allow_live: bool = True,
              callback: Callable[[PricingStatus], None] | None = None) -> PricingCatalogue:
    global active
    active = PricingCatalogue(cache_path, allow_live=allow_live)
    active.callback = callback
    return active
