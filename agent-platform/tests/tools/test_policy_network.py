from __future__ import annotations

import pytest

from runtime.policy_engine import PolicyEngine
from runtime.types import PolicyViolation


@pytest.fixture
def open_network(config, tmp_path):
    """Allow any domain so the private-network (SSRF) check is what gets exercised."""
    policies = {**config.policies, "network": {**config.policies["network"], "default": "allow"}}
    return PolicyEngine(policies, tmp_path)


@pytest.mark.parametrize(
    "url, reason",
    [
        ("http://docs.python.org/3/", "scheme"),
        ("https://evil.example.com/", "allow_domains"),
        ("https://docs.python.org.evil.com/", "allow_domains"),
        ("https://169.254.169.254/latest/meta-data/", "allow_domains"),
    ],
)
def test_default_network_policy_refuses(config, tmp_path, url, reason):
    with pytest.raises(PolicyViolation, match=reason):
        PolicyEngine(config.policies, tmp_path).check_url(url)


@pytest.mark.parametrize(
    "url",
    ["https://127.0.0.1/", "https://10.1.2.3/", "https://192.168.0.10/", "https://169.254.169.254/", "https://[::1]/", "https://localhost/"],
)
def test_private_networks_are_refused_even_when_domains_are_open(open_network, url):
    with pytest.raises(PolicyViolation):
        open_network.check_url(url)


def test_redaction_masks_known_secret_formats(config, tmp_path):
    policy = PolicyEngine(config.policies, tmp_path)
    text = "key sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345 and ghp_" + "a" * 36
    redacted = policy.redact(text)
    assert "sk-ant-api03" not in redacted
    assert "[REDACTED:anthropic_api_key]" in redacted
    assert "[REDACTED:github_token]" in redacted
