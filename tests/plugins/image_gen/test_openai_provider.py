"""Tests for the bundled OpenAI image_gen plugin (gpt-image-2, three tiers)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

import plugins.image_gen.openai as openai_plugin


# 1×1 transparent PNG — valid bytes for save_b64_image()
_PNG_HEX = (
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6300010000000500010d0a2db40000000049454e44"
    "ae426082"
)


def _b64_png() -> str:
    import base64
    return base64.b64encode(bytes.fromhex(_PNG_HEX)).decode()


def _fake_response(*, b64=None, url=None, revised_prompt=None):
    item = SimpleNamespace(b64_json=b64, url=url, revised_prompt=revised_prompt)
    return SimpleNamespace(data=[item])


@pytest.fixture(autouse=True)
def _tmp_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.delenv("IMAGE_GEN_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    return openai_plugin.OpenAIImageGenProvider()


def _patched_openai(fake_client: MagicMock):
    fake_openai = MagicMock()
    fake_openai.OpenAI.return_value = fake_client
    return patch.dict("sys.modules", {"openai": fake_openai})


# ── Metadata ────────────────────────────────────────────────────────────────


class TestMetadata:
    def test_name(self, provider):
        assert provider.name == "openai"

    def test_default_model(self, provider):
        assert provider.default_model() == "gpt-image-2-medium"

    def test_list_models_three_tiers(self, provider):
        ids = [m["id"] for m in provider.list_models()]
        assert ids == ["gpt-image-2-low", "gpt-image-2-medium", "gpt-image-2-high"]

    def test_catalog_entries_have_display_speed_strengths(self, provider):
        for entry in provider.list_models():
            assert entry["display"].startswith("GPT Image 2")
            assert entry["speed"]
            assert entry["strengths"]

    def test_setup_metadata_uses_the_dedicated_image_credential(self, provider):
        setup_keys = [entry["key"] for entry in provider.get_setup_schema()["env_vars"]]
        manifest = yaml.safe_load(
            (Path(openai_plugin.__file__).with_name("plugin.yaml")).read_text(
                encoding="utf-8"
            )
        )

        assert setup_keys == ["IMAGE_GEN_OPENAI_API_KEY"]
        assert manifest["requires_env"] == setup_keys


# ── Availability ────────────────────────────────────────────────────────────


class TestAvailability:
    def test_no_api_key_unavailable(self, monkeypatch):
        monkeypatch.delenv("IMAGE_GEN_OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert openai_plugin.OpenAIImageGenProvider().is_available() is False

    def test_api_key_set_available(self, monkeypatch):
        monkeypatch.delenv("IMAGE_GEN_OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        assert openai_plugin.OpenAIImageGenProvider().is_available() is True

    def test_dedicated_image_key_is_available_without_chat_key(self, monkeypatch):
        monkeypatch.setenv("IMAGE_GEN_OPENAI_API_KEY", "image-test-key")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert openai_plugin.OpenAIImageGenProvider().is_available() is True


# ── Model resolution ────────────────────────────────────────────────────────


class TestModelResolution:

    def test_env_var_override(self, monkeypatch):
        monkeypatch.setenv("OPENAI_IMAGE_MODEL", "gpt-image-2-high")
        model_id, meta = openai_plugin._resolve_model()
        assert model_id == "gpt-image-2-high"
        assert meta["quality"] == "high"


    def test_config_openai_model(self, tmp_path):
        import yaml
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"image_gen": {"openai": {"model": "gpt-image-2-low"}}})
        )
        model_id, meta = openai_plugin._resolve_model()
        assert model_id == "gpt-image-2-low"
        assert meta["quality"] == "low"


# ── Generate ────────────────────────────────────────────────────────────────


class TestSourceImageLoading:
    def test_load_image_bytes_blocks_credential_store(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        auth_json = hermes_home / "auth.json"
        auth_json.write_text('{"api_key":"sk-secret"}', encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        with pytest.raises(ValueError, match="credential store"):
            openai_plugin._load_image_bytes(str(auth_json))


    def test_load_image_bytes_allows_legit_local_image(self, tmp_path, monkeypatch):
        """Negative control: a legitimate local image path is NOT blocked and
        loads normally — proves the guard doesn't over-fire on everything."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        img = tmp_path / "pic.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nfake-image-bytes")

        data, name = openai_plugin._load_image_bytes(str(img))
        assert data == b"\x89PNG\r\n\x1a\nfake-image-bytes"
        assert name == "pic.png"


class TestGenerate:
    def test_empty_prompt_rejected(self, provider):
        result = provider.generate("", aspect_ratio="square")
        assert result["success"] is False
        assert result["error_type"] == "invalid_argument"

    def test_missing_api_key(self, monkeypatch):
        monkeypatch.delenv("IMAGE_GEN_OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        result = openai_plugin.OpenAIImageGenProvider().generate("a cat")
        assert result["success"] is False
        assert result["error_type"] == "auth_required"
        assert "IMAGE_GEN_OPENAI_API_KEY" in result["error"]

    def test_dedicated_relay_configuration_builds_zero_retry_client(
        self, tmp_path, monkeypatch,
    ):
        (tmp_path / "config.yaml").write_text(
            "image_gen:\n"
            "  provider: openai\n"
            "  openai:\n"
            "    base_url: https://relay.example/v1/\n"
            "    model: gpt-image-1.5\n"
            "    quality: high\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("IMAGE_GEN_OPENAI_API_KEY", "image-secret")
        monkeypatch.setenv("OPENAI_API_KEY", "chat-secret")
        fake_client = MagicMock()
        fake_client.images.generate.return_value = _fake_response(b64=_b64_png())
        fake_openai = MagicMock()
        fake_openai.OpenAI.return_value = fake_client

        with patch.dict("sys.modules", {"openai": fake_openai}):
            result = openai_plugin.OpenAIImageGenProvider().generate("a red square")

        assert result["success"] is True
        assert result["model"] == "gpt-image-1.5"
        fake_openai.OpenAI.assert_called_once_with(
            api_key="image-secret",
            base_url="https://relay.example/v1",
            max_retries=0,
        )
        call_kwargs = fake_client.images.generate.call_args.kwargs
        assert call_kwargs["model"] == "gpt-image-1.5"
        assert call_kwargs["quality"] == "high"

    def test_legacy_chat_key_fallback_disables_sdk_retries(self, monkeypatch):
        monkeypatch.delenv("IMAGE_GEN_OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "legacy-key")
        fake_client = MagicMock()
        fake_client.images.generate.return_value = _fake_response(b64=_b64_png())
        fake_openai = MagicMock()
        fake_openai.OpenAI.return_value = fake_client

        with patch.dict("sys.modules", {"openai": fake_openai}):
            result = openai_plugin.OpenAIImageGenProvider().generate("a cat")

        assert result["success"] is True
        fake_openai.OpenAI.assert_called_once_with(
            api_key="legacy-key",
            max_retries=0,
        )

    def test_malformed_relay_url_fails_before_client_creation(
        self, tmp_path, monkeypatch,
    ):
        (tmp_path / "config.yaml").write_text(
            "image_gen:\n"
            "  provider: openai\n"
            "  openai:\n"
            "    base_url: relay.example/v1\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("IMAGE_GEN_OPENAI_API_KEY", "image-secret")
        fake_openai = MagicMock()

        with patch.dict("sys.modules", {"openai": fake_openai}):
            result = openai_plugin.OpenAIImageGenProvider().generate("a cat")

        assert result["success"] is False
        assert result["error_type"] == "invalid_configuration"
        fake_openai.OpenAI.assert_not_called()

    @pytest.mark.parametrize(
        "base_url",
        [
            "https://user:password@relay.example/v1",
            "https://relay.example/v1?api_key=secret",
            "https://relay.example/v1#fragment",
        ],
    )
    def test_credential_bearing_relay_url_fails_before_client_creation(
        self, base_url, tmp_path, monkeypatch,
    ):
        (tmp_path / "config.yaml").write_text(
            "image_gen:\n"
            "  provider: openai\n"
            "  openai:\n"
            f"    base_url: {base_url}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("IMAGE_GEN_OPENAI_API_KEY", "image-secret")
        fake_openai = MagicMock()

        with patch.dict("sys.modules", {"openai": fake_openai}):
            result = openai_plugin.OpenAIImageGenProvider().generate("a cat")

        assert result["success"] is False
        assert result["error_type"] == "invalid_configuration"
        assert "secret" not in result["error"]
        fake_openai.OpenAI.assert_not_called()

    def test_upstream_error_is_redacted_from_result_and_logs_and_not_retried(
        self, tmp_path, monkeypatch, caplog,
    ):
        caplog.set_level("DEBUG", logger=openai_plugin.__name__)
        secret = "image-secret-must-not-leak"
        monkeypatch.setenv("IMAGE_GEN_OPENAI_API_KEY", secret)
        fake_client = MagicMock()
        fake_client.images.generate.side_effect = RuntimeError(
            f"Authorization: Bearer {secret}; raw upstream body"
        )
        fake_openai = MagicMock()
        fake_openai.OpenAI.return_value = fake_client

        with patch.dict("sys.modules", {"openai": fake_openai}):
            result = openai_plugin.OpenAIImageGenProvider().generate("a cat")

        assert result["success"] is False
        assert result["error_type"] == "api_error"
        assert secret not in result["error"]
        assert "Authorization" not in result["error"]
        assert "raw upstream body" not in result["error"]
        assert secret not in caplog.text
        assert "Authorization" not in caplog.text
        assert "raw upstream body" not in caplog.text
        assert fake_client.images.generate.call_count == 1

    def test_edit_uses_dedicated_relay_model(self, tmp_path, monkeypatch):
        (tmp_path / "config.yaml").write_text(
            "image_gen:\n"
            "  provider: openai\n"
            "  openai:\n"
            "    base_url: https://relay.example/v1\n"
            "    model: gpt-image-1.5\n"
            "    quality: medium\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("IMAGE_GEN_OPENAI_API_KEY", "image-secret")
        source = tmp_path / "source.png"
        source.write_bytes(bytes.fromhex(_PNG_HEX))
        fake_client = MagicMock()
        fake_client.images.edit.return_value = _fake_response(b64=_b64_png())
        fake_openai = MagicMock()
        fake_openai.OpenAI.return_value = fake_client

        with patch.dict("sys.modules", {"openai": fake_openai}):
            result = openai_plugin.OpenAIImageGenProvider().generate(
                "make it blue", image_url=str(source)
            )

        assert result["success"] is True
        fake_openai.OpenAI.assert_called_once_with(
            api_key="image-secret",
            base_url="https://relay.example/v1",
            max_retries=0,
        )
        assert fake_client.images.edit.call_args.kwargs["model"] == "gpt-image-1.5"

    def test_edit_upstream_error_is_redacted_from_result_and_logs(
        self, tmp_path, monkeypatch, caplog,
    ):
        caplog.set_level("DEBUG", logger=openai_plugin.__name__)
        secret = "edit-secret-must-not-leak"
        monkeypatch.setenv("IMAGE_GEN_OPENAI_API_KEY", secret)
        source = tmp_path / "source.png"
        source.write_bytes(bytes.fromhex(_PNG_HEX))
        fake_client = MagicMock()
        fake_client.images.edit.side_effect = RuntimeError(
            f"Authorization: Bearer {secret}; raw edit body"
        )
        fake_openai = MagicMock()
        fake_openai.OpenAI.return_value = fake_client

        with patch.dict("sys.modules", {"openai": fake_openai}):
            result = openai_plugin.OpenAIImageGenProvider().generate(
                "make it blue", image_url=str(source)
            )

        assert result["success"] is False
        assert result["error_type"] == "api_error"
        combined = result["error"] + caplog.text
        assert secret not in combined
        assert "Authorization" not in combined
        assert "raw edit body" not in combined
        assert fake_client.images.edit.call_count == 1

    def test_b64_saves_to_cache(self, provider, tmp_path):
        png_bytes = bytes.fromhex(_PNG_HEX)
        fake_client = MagicMock()
        fake_client.images.generate.return_value = _fake_response(b64=_b64_png())

        with _patched_openai(fake_client):
            result = provider.generate("a cat", aspect_ratio="landscape")

        assert result["success"] is True
        assert result["model"] == "gpt-image-2-medium"
        assert result["aspect_ratio"] == "landscape"
        assert result["provider"] == "openai"
        assert result["quality"] == "medium"

        saved = Path(result["image"])
        assert saved.exists()
        assert saved.parent == tmp_path / "cache" / "images"
        assert saved.read_bytes() == png_bytes

        call_kwargs = fake_client.images.generate.call_args.kwargs
        # All tiers hit the single underlying API model.
        assert call_kwargs["model"] == "gpt-image-2"
        assert call_kwargs["quality"] == "medium"
        assert call_kwargs["size"] == "1536x1024"
        # gpt-image-2 rejects response_format — we must NOT send it.
        assert "response_format" not in call_kwargs

    @pytest.mark.parametrize("tier,expected_quality", [
        ("gpt-image-2-low", "low"),
        ("gpt-image-2-medium", "medium"),
        ("gpt-image-2-high", "high"),
    ])
    def test_tier_maps_to_quality(self, provider, monkeypatch, tier, expected_quality):
        monkeypatch.setenv("OPENAI_IMAGE_MODEL", tier)
        fake_client = MagicMock()
        fake_client.images.generate.return_value = _fake_response(b64=_b64_png())

        with _patched_openai(fake_client):
            result = provider.generate("a cat")

        assert result["model"] == tier
        assert result["quality"] == expected_quality
        assert fake_client.images.generate.call_args.kwargs["quality"] == expected_quality
        # Always the same underlying API model regardless of tier.
        assert fake_client.images.generate.call_args.kwargs["model"] == "gpt-image-2"

    @pytest.mark.parametrize("aspect,expected_size", [
        ("landscape", "1536x1024"),
        ("square", "1024x1024"),
        ("portrait", "1024x1536"),
    ])
    def test_aspect_ratio_mapping(self, provider, aspect, expected_size):
        fake_client = MagicMock()
        fake_client.images.generate.return_value = _fake_response(b64=_b64_png())

        with _patched_openai(fake_client):
            provider.generate("a cat", aspect_ratio=aspect)

        assert fake_client.images.generate.call_args.kwargs["size"] == expected_size

    def test_revised_prompt_passed_through(self, provider):
        fake_client = MagicMock()
        fake_client.images.generate.return_value = _fake_response(
            b64=_b64_png(), revised_prompt="A photo of a cat",
        )

        with _patched_openai(fake_client):
            result = provider.generate("a cat")

        assert result["revised_prompt"] == "A photo of a cat"


    def test_url_response_is_cached_locally(self, provider):
        """OpenAI URL response (if API ever returns one) is cached locally.

        Pre-fix this asserted the bare URL passed through; symmetric to the
        xAI #26942 fix.  Even though gpt-image-2 returns b64 today, every
        ``image_gen`` provider must guarantee the gateway gets a stable
        file path so ephemeral signed URLs can't expire mid-flight.
        """
        fake_client = MagicMock()
        fake_client.images.generate.return_value = _fake_response(
            b64=None, url="https://example.com/img.png",
        )

        with _patched_openai(fake_client), patch(
            "plugins.image_gen.openai.save_url_image",
            return_value=Path("/tmp/openai_gpt-image-2_20260524_000000_deadbeef.png"),
        ) as mock_save_url:
            result = provider.generate("a cat")

        assert result["success"] is True
        assert result["image"].startswith("/")
        assert "example.com" not in result["image"]
        mock_save_url.assert_called_once()

    def test_url_cache_failure_logs_only_the_safe_host(self, provider, caplog):
        signed_url = (
            "https://cdn.example.com/private/generated.png"
            "?token=relay-secret&signature=signed-secret"
        )
        fake_client = MagicMock()
        fake_client.images.generate.return_value = _fake_response(
            b64=None,
            url=signed_url,
        )
        caplog.set_level("WARNING", logger=openai_plugin.__name__)

        with _patched_openai(fake_client), patch(
            "plugins.image_gen.openai.save_url_image",
            side_effect=RuntimeError("download failed with credential-secret"),
        ):
            result = provider.generate("a cat")

        assert result["success"] is True
        assert result["image"] == signed_url
        assert "cdn.example.com" in caplog.text
        assert "/private/generated.png" not in caplog.text
        assert "relay-secret" not in caplog.text
        assert "signed-secret" not in caplog.text
        assert "credential-secret" not in caplog.text
