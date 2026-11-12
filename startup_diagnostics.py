"""Safe startup error formatting; no environment, request data or network access."""

def startup_failure_message(error: Exception) -> str:
    """Report only a known API method, never request data or exception text."""
    method = getattr(getattr(error, "method", None), "__api_method__", None)
    safe_methods = {
        "getMe", "getWebhookInfo", "getChat", "getChatMember", "getChatMemberCount",
        "setMyCommands", "setChatMenuButton", "getUpdates",
    }
    suffix = f" [{method}]" if isinstance(method, str) and method in safe_methods else ""
    # Classify locally; never emit Telegram's free-form error message or payload.
    message = getattr(error, "message", None)
    if isinstance(message, str):
        normalized = message.casefold()
        for fragment, code in (
            ("user not found", "USER_NOT_FOUND"),
            ("chat not found", "CHAT_NOT_FOUND"),
            ("not enough rights", "INSUFFICIENT_RIGHTS"),
        ):
            if fragment in normalized:
                suffix += f" [{code}]"
                break
    return f"Test startup failed: {type(error).__name__}{suffix}"

