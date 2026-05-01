"""Tests for SSRF validation in tool_executor._validate_webhook_url()."""

from __future__ import annotations

import pytest

from getpatter.services.tool_executor import _validate_webhook_url


class TestValidateWebhookUrl:
    """SSRF protection: _validate_webhook_url blocks dangerous URLs."""

    def test_valid_https_url_passes(self) -> None:
        _validate_webhook_url("https://example.com/webhook")

    def test_valid_http_url_passes(self) -> None:
        _validate_webhook_url("http://example.com/webhook")

    def test_private_ip_127_blocked(self) -> None:
        with pytest.raises(ValueError, match="private"):
            _validate_webhook_url("http://127.0.0.1/webhook")

    def test_private_ip_192_168_blocked(self) -> None:
        with pytest.raises(ValueError, match="private"):
            _validate_webhook_url("http://192.168.1.1/webhook")

    def test_private_ip_10_blocked(self) -> None:
        with pytest.raises(ValueError, match="private"):
            _validate_webhook_url("http://10.0.0.1/webhook")

    def test_loopback_ipv6_blocked(self) -> None:
        with pytest.raises(ValueError, match="private|reserved"):
            _validate_webhook_url("http://[::1]/webhook")

    def test_link_local_blocked(self) -> None:
        with pytest.raises(ValueError, match="private|reserved"):
            _validate_webhook_url("http://169.254.1.1/webhook")

    def test_ftp_scheme_rejected(self) -> None:
        with pytest.raises(ValueError, match="Invalid URL scheme"):
            _validate_webhook_url("ftp://example.com/file")

    def test_file_scheme_rejected(self) -> None:
        with pytest.raises(ValueError, match="Invalid URL scheme"):
            _validate_webhook_url("file:///etc/passwd")

    def test_missing_hostname_rejected(self) -> None:
        with pytest.raises(ValueError, match="missing a hostname"):
            _validate_webhook_url("http:///path")

    def test_normal_hostname_passes(self) -> None:
        _validate_webhook_url("https://api.acme.io/hooks/tool")
