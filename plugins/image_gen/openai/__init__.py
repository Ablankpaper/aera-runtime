"""OpenAI image generation backend.

Exposes OpenAI's ``gpt-image-2`` model at three quality tiers as an
:class:`ImageGenProvider` implementation. The tiers are implemented as
three virtual model IDs so the ``hermes tools`` model picker and the
``image_gen.model`` config key behave like any other multi-model backend:

    gpt-image-2-low     ~15s   fastest, good for iteration
    gpt-image-2-medium  ~40s   default — balanced
    gpt-image-2-high    ~2min  slowest, highest fidelity

All three hit the same underlying API model (``gpt-image-2``) with a
different ``quality`` parameter. Output is base64 JSON → saved under
``$HERMES_HOME/cache/images/``.

Selection precedence (first hit wins):

1. ``OPENAI_IMAGE_MODEL`` env var (escape hatch for scripts / tests)
2. ``image_gen.openai.model`` in ``config.yaml``
3. ``image_gen.model`` in ``config.yaml`` (when it's one of our tier IDs)
4. :data:`DEFAULT_MODEL` — ``gpt-image-2-medium``
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from agent.secret_scope import get_secret
from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    normalize_reference_images,
    resolve_aspect_ratio,
    save_b64_image,
    save_url_image,
    success_response,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------
#
# All three IDs resolve to the same underlying API model with a different
# ``quality`` setting. ``api_model`` is what gets sent to OpenAI;
# ``quality`` is the knob that changes generation time and output fidelity.

API_MODEL = "gpt-image-2"

_MODELS: Dict[str, Dict[str, Any]] = {
    "gpt-image-2-low": {
        "display": "GPT Image 2 (Low)",
        "speed": "~15s",
        "strengths": "Fast iteration, lowest cost",
        "quality": "low",
    },
    "gpt-image-2-medium": {
        "display": "GPT Image 2 (Medium)",
        "speed": "~40s",
        "strengths": "Balanced — default",
        "quality": "medium",
    },
    "gpt-image-2-high": {
        "display": "GPT Image 2 (High)",
        "speed": "~2min",
        "strengths": "Highest fidelity, strongest prompt adherence",
        "quality": "high",
    },
}

DEFAULT_MODEL = "gpt-image-2-medium"

_SIZES = {
    "landscape": "1536x1024",
    "square": "1024x1024",
    "portrait": "1024x1536",
}


def _load_openai_config() -> Dict[str, Any]:
    """Read ``image_gen`` from config.yaml (returns {} on any failure)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:
        logger.debug("Could not load image_gen config: %s", exc)
        return {}


def _resolve_model() -> Tuple[str, Dict[str, Any]]:
    """Decide which tier to use and return ``(model_id, meta)``."""
    env_override = os.environ.get("OPENAI_IMAGE_MODEL")
    if env_override and env_override.strip():
        candidate = env_override.strip()
        if candidate in _MODELS:
            return candidate, _MODELS[candidate]
        return candidate, {
            "display": candidate,
            "speed": "varies",
            "strengths": "Configured OpenAI-compatible image model",
            "quality": "medium",
            "api_model": candidate,
        }

    cfg = _load_openai_config()
    raw_openai_cfg = cfg.get("openai")
    openai_cfg: Dict[str, Any] = (
        raw_openai_cfg if isinstance(raw_openai_cfg, dict) else {}
    )
    candidate: Optional[str] = None
    value = openai_cfg.get("model")
    if isinstance(value, str) and value.strip():
        candidate = value.strip()
    if candidate is None:
        top = cfg.get("model")
        if isinstance(top, str) and top.strip():
            candidate = top.strip()

    if candidate is not None:
        if candidate in _MODELS:
            meta = dict(_MODELS[candidate])
        else:
            meta = {
                "display": candidate,
                "speed": "varies",
                "strengths": "Configured OpenAI-compatible image model",
                "quality": "medium",
                "api_model": candidate,
            }
        configured_quality = openai_cfg.get("quality") or cfg.get("quality")
        if isinstance(configured_quality, str) and configured_quality in {"low", "medium", "high"}:
            meta["quality"] = configured_quality
        return candidate, meta

    return DEFAULT_MODEL, _MODELS[DEFAULT_MODEL]


def _resolve_api_key() -> Optional[str]:
    """Resolve the image credential without coupling it to chat routing."""
    return get_secret("IMAGE_GEN_OPENAI_API_KEY") or get_secret("OPENAI_API_KEY")


def _resolve_base_url() -> Optional[str]:
    """Return a validated OpenAI-compatible base URL, or None for SDK default."""
    cfg = _load_openai_config()
    raw_openai_cfg = cfg.get("openai")
    openai_cfg: Dict[str, Any] = (
        raw_openai_cfg if isinstance(raw_openai_cfg, dict) else {}
    )
    raw = openai_cfg.get("base_url")
    if raw is None:
        raw = cfg.get("base_url")
    if raw is None or not str(raw).strip():
        return None
    value = str(raw).strip().rstrip("/")
    parsed = urlparse(value)
    if (
        len(value) > 2048
        or parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Image generation base URL must be an absolute credential-free http(s) URL"
        )
    return value


def _safe_provider_error(action: str) -> str:
    """Map provider failures to a bounded message without copying raw bodies."""
    return f"OpenAI image {action} failed; the upstream request was not completed"


def _safe_url_host(value: object) -> str:
    """Return a bounded host for diagnostics without exposing a signed URL."""
    try:
        host = urlparse(str(value)).hostname
    except (TypeError, ValueError):
        return "unknown"
    if not host or len(host) > 255:
        return "unknown"
    return host


# ---------------------------------------------------------------------------
# Source-image loading (for image-to-image / edit)
# ---------------------------------------------------------------------------


def _load_image_bytes(ref: str) -> Tuple[bytes, str]:
    """Load image bytes from a URL or local file path.

    Returns ``(data, filename)``. Raises on any network / IO error so the
    caller can surface a clean error_response.
    """
    ref = ref.strip()
    lower = ref.lower()
    if lower.startswith(("http://", "https://")):
        import requests

        resp = requests.get(ref, timeout=60)
        resp.raise_for_status()
        name = ref.split("?", 1)[0].rsplit("/", 1)[-1] or "image.png"
        return resp.content, name
    if lower.startswith("data:"):
        import base64

        header, _, b64 = ref.partition(",")
        ext = "png"
        if "image/" in header:
            ext = header.split("image/", 1)[1].split(";", 1)[0] or "png"
        return base64.b64decode(b64), f"image.{ext}"
    # Local file path — enforce the shared credential-read guard before reading.
    from agent.file_safety import raise_if_read_blocked

    raise_if_read_blocked(ref)
    with open(ref, "rb") as fh:
        data = fh.read()
    name = os.path.basename(ref) or "image.png"
    return data, name


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class OpenAIImageGenProvider(ImageGenProvider):
    """OpenAI ``images.generate`` / ``images.edit`` backend — gpt-image-2."""

    @property
    def name(self) -> str:
        return "openai"

    @property
    def display_name(self) -> str:
        return "OpenAI"

    def is_available(self) -> bool:
        if not _resolve_api_key():
            return False
        try:
            import openai  # noqa: F401
        except ImportError:
            return False
        return True

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": model_id,
                "display": meta["display"],
                "speed": meta["speed"],
                "strengths": meta["strengths"],
                "price": "varies",
            }
            for model_id, meta in _MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "OpenAI",
            "badge": "paid",
            "tag": "gpt-image-2 at low/medium/high quality tiers — text-to-image & image editing",
            "env_vars": [
                {
                    "key": "IMAGE_GEN_OPENAI_API_KEY",
                    "prompt": "OpenAI-compatible image API key",
                    "url": "https://platform.openai.com/api-keys",
                },
            ],
        }

    def capabilities(self) -> Dict[str, Any]:
        # gpt-image-2 supports editing via images.edit() with up to 16 source
        # images.
        return {"modalities": ["text", "image"], "max_reference_images": 16}

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)

        if not prompt:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument",
                provider="openai",
                aspect_ratio=aspect,
            )

        api_key = _resolve_api_key()
        if not api_key:
            return error_response(
                error=(
                    "IMAGE_GEN_OPENAI_API_KEY not set. Run `hermes tools` → "
                    "Image Generation → OpenAI to configure it. Existing "
                    "OPENAI_API_KEY credentials remain a compatibility fallback."
                ),
                error_type="auth_required",
                provider="openai",
                aspect_ratio=aspect,
            )

        try:
            import openai
        except ImportError:
            return error_response(
                error="openai Python package not installed (pip install openai)",
                error_type="missing_dependency",
                provider="openai",
                aspect_ratio=aspect,
            )

        tier_id, meta = _resolve_model()
        size = _SIZES.get(aspect, _SIZES["square"])
        try:
            base_url = _resolve_base_url()
        except ValueError as exc:
            return error_response(
                error=str(exc),
                error_type="invalid_configuration",
                provider="openai",
                model=tier_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        api_model = str(meta.get("api_model") or (API_MODEL if tier_id in _MODELS else tier_id))

        # Collect source images (primary + references) for image-to-image.
        sources: List[str] = []
        if isinstance(image_url, str) and image_url.strip():
            sources.append(image_url.strip())
        for ref in (normalize_reference_images(reference_image_urls) or []):
            sources.append(ref)
        sources = sources[:16]  # gpt-image-2 edit caps at 16 images
        is_edit = bool(sources)
        modality = "image" if is_edit else "text"

        client_kwargs: Dict[str, Any] = {"api_key": api_key, "max_retries": 0}
        if base_url:
            client_kwargs["base_url"] = base_url
        try:
            client = openai.OpenAI(**client_kwargs)
        except Exception:
            logger.debug("OpenAI image client construction failed")
            return error_response(
                error=_safe_provider_error("client construction"),
                error_type="api_error",
                provider="openai",
                model=tier_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        if is_edit:
            # images.edit() expects file-like objects. Download/read each
            # source into a named BytesIO so the SDK sends correct multipart.
            import io

            try:
                files = []
                for ref in sources:
                    data, fname = _load_image_bytes(ref)
                    bio = io.BytesIO(data)
                    bio.name = fname
                    files.append(bio)
            except Exception:
                logger.debug("OpenAI source image loading failed")
                return error_response(
                    error="Could not load source image for editing",
                    error_type="io_error",
                    provider="openai",
                    model=tier_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )

            try:
                response = client.images.edit(
                    model=api_model,
                    image=files if len(files) > 1 else files[0],
                    prompt=prompt,
                    size=size,  # type: ignore[arg-type]  # _SIZES values are valid gpt-image sizes
                    quality=meta["quality"],
                    n=1,
                )
            except Exception:
                logger.debug("OpenAI image edit request failed")
                return error_response(
                    error=_safe_provider_error("editing"),
                    error_type="api_error",
                    provider="openai",
                    model=tier_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )
        else:
            # gpt-image-2 returns b64_json unconditionally and REJECTS
            # ``response_format`` as an unknown parameter. Don't send it.
            payload: Dict[str, Any] = {
                "model": api_model,
                "prompt": prompt,
                "size": size,
                "n": 1,
                "quality": meta["quality"],
            }

            try:
                response = client.images.generate(**payload)
            except Exception:
                logger.debug("OpenAI image generation request failed")
                return error_response(
                    error=_safe_provider_error("generation"),
                    error_type="api_error",
                    provider="openai",
                    model=tier_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )

        data = getattr(response, "data", None) or []
        if not data:
            return error_response(
                error="OpenAI returned no image data",
                error_type="empty_response",
                provider="openai",
                model=tier_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        first = data[0]
        b64 = getattr(first, "b64_json", None)
        url = getattr(first, "url", None)
        revised_prompt = getattr(first, "revised_prompt", None)

        if b64:
            try:
                saved_path = save_b64_image(b64, prefix=f"openai_{tier_id}")
            except Exception as exc:
                return error_response(
                    error=f"Could not save image to cache: {exc}",
                    error_type="io_error",
                    provider="openai",
                    model=tier_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )
            image_ref = str(saved_path)
        elif url:
            # Defensive — gpt-image-2 returns b64 today, but OpenAI's API
            # has previously returned URLs.  Cache the bytes locally so the
            # gateway never tries to fetch an ephemeral / signed URL after
            # it expires — same rationale as the xAI provider (#26942).
            try:
                saved_path = save_url_image(url, prefix=f"openai_{tier_id}")
            except Exception:
                logger.warning(
                    "OpenAI image response from host %s could not be cached; "
                    "falling back to bare URL.",
                    _safe_url_host(url),
                )
                image_ref = url
            else:
                image_ref = str(saved_path)
        else:
            return error_response(
                error="OpenAI response contained neither b64_json nor URL",
                error_type="empty_response",
                provider="openai",
                model=tier_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        extra: Dict[str, Any] = {"size": size, "quality": meta["quality"]}
        if revised_prompt:
            extra["revised_prompt"] = revised_prompt

        return success_response(
            image=image_ref,
            model=tier_id,
            prompt=prompt,
            aspect_ratio=aspect,
            provider="openai",
            modality=modality,
            extra=extra,
        )


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Plugin entry point — wire ``OpenAIImageGenProvider`` into the registry."""
    ctx.register_image_gen_provider(OpenAIImageGenProvider())
