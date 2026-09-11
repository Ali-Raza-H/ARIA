from pathlib import Path

from aria.config import DockerConfig, GmailConfig, MediaConfig, SystemConfig, load_config
from aria.tools import ToolContext, ToolRegistry, register_gmail_tools, register_system_tools
from aria.tools.core.confirmation import ConfirmationManager


def test_confirmation_accepts_direct_conversational_service_request() -> None:
    manager = ConfirmationManager()
    assert manager.require(
        "systemctl",
        {"action": "stop", "service": "nginx"},
        "stop nginx",
        "Please stop nginx now",
    ) is None


def test_confirmation_requires_follow_up_for_inferred_action() -> None:
    manager = ConfirmationManager()
    args = {"action": "restart", "service": "nginx"}
    message = manager.require("systemctl", args, "restart nginx", "Check whether nginx is healthy")
    assert message is not None and "CONFIRMATION_REQUIRED" in message
    assert manager.require("systemctl", args, "restart nginx", "yes") is None


def test_gmail_tools_have_explicit_names_and_no_secrets_in_schema() -> None:
    registry = ToolRegistry()
    register_gmail_tools(registry, GmailConfig(enabled=True))
    names = registry.names()
    assert "gmail_search_threads" in names
    assert "gmail_send" in names
    assert "gmail_contacts" in names
    assert "GMAIL_API_ACCESS_TOKEN" not in str(registry.schemas())


def test_system_tools_are_registered_only_for_enabled_sections() -> None:
    registry = ToolRegistry()
    register_system_tools(
        registry,
        SystemConfig(enabled=True),
        MediaConfig(enabled=False),
        DockerConfig(enabled=False),
    )
    assert "system_monitor" in registry.names()
    assert "systemctl_control" in registry.names()
    assert "media_status" not in registry.names()
    assert "docker_control" not in registry.names()


def test_config_example_has_new_gmail_fields() -> None:
    root = Path(__file__).resolve().parents[2]
    config = load_config(root / "config.example.yaml", root)
    assert config.gmail.people_api_base_url == "https://people.googleapis.com/v1"
    assert config.gmail.oauth_token_path == root / "data/gmail/oauth-token.json"
