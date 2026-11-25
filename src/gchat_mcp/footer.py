"""Optional message footer: configuration rules and the exact append/no-double behaviour."""
ENV = "GCHAT_MCP_FOOTER"
OFF_VALUES = {"", "off", "none"}


def clean(raw: str | None) -> str:
    """Canonical configured footer: stripped, with empty/'off' meaning disabled."""
    text = (raw or "").strip()
    return "" if text.lower() in OFF_VALUES else text


def configured(environ: dict[str, str], stored: str) -> str:
    """The effective footer template. The environment wins whenever the variable exists, even empty/off."""
    return clean(environ[ENV]) if ENV in environ else clean(stored)


def expand(template: str, user: str) -> str:
    return template.replace("{user}", user)


def append(text: str, footer: str) -> tuple[str, bool]:
    """Append `footer` once, separated by a blank line; second value says this call added it."""
    if not footer or text.rstrip().endswith(footer):
        return text, False
    return (f"{text.rstrip()}\n\n" if text.strip() else "") + footer, True
