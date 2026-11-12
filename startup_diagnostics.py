"""Safe startup error formatting; no environment, request data or network access."""

def startup_failure_message(error: Exception) -> str:
    """Report only a known API method, never request data or exception text."""
    method = getattr(getattr(error, "method", None), "__api_method__", None)
    safe_methods = {
        "getMe", "getWebhookInfo", "getChat", "getChatMember", "getChatMemberCount",
        "setMyCommands", "setChatMenuButton", "getUpdates",
    }
    suffix = f" [{method}]" if isinstance(method, str) and method in safe_methods else ""
    return f"Test startup failed: {type(error).__name__}{suffix}"

