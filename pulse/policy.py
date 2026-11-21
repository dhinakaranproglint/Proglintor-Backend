"""Weekend work policy (mirrors proglint-pulse/src/shared/policy.js)."""

DEFAULT_DEV_TOOLS = [
    # Editors & IDEs
    "Visual Studio Code", "com.microsoft.VSCode", "code", "code.exe", "Cursor", "Xcode", "Android Studio",
    "IntelliJ IDEA", "PyCharm", "WebStorm", "GoLand", "PhpStorm", "Rider", "CLion", "DataGrip", "RubyMine",
    "Visual Studio", "devenv.exe", "Eclipse", "NetBeans", "Sublime Text", "Zed", "Fleet", "Nova", "Atom",
    # Terminals
    "Terminal", "com.apple.Terminal", "iTerm", "Warp", "Alacritty", "kitty", "Hyper", "WindowsTerminal.exe",
    "powershell.exe", "cmd.exe", "gnome-terminal-server", "konsole", "tilix",
    # Dev utilities
    "Postman", "Insomnia", "Docker", "Docker Desktop", "GitHub Desktop", "Sourcetree", "Fork", "GitKraken",
    "pgAdmin", "DBeaver", "TablePlus", "MongoDB Compass", "Sequel Ace", "Beekeeper Studio",
]

DEFAULT_REASONS = [
    "Urgent production issue",
    "Project deadline",
    "Approved by my manager",
    "Learning or personal project",
    "Other",
]

DEFAULT_POLICY = {
    "weekendCheck": True,
    "weekendDays": [0, 6],  # Sunday, Saturday (JavaScript getDay numbering)
    "devTools": DEFAULT_DEV_TOOLS,
    "reasons": DEFAULT_REASONS,
    "message": (
        "Proglint encourages a healthy work–life balance. Weekend work should be urgent or approved by "
        "your manager. Please tell us why you are working today — your answer is shared with your Proglint admin."
    ),
}


def normalize_policy(p):
    src = p if isinstance(p, dict) else {}

    def str_list(value, default):
        if not isinstance(value, list):
            return list(default)
        return [str(x).strip()[:200] for x in value if str(x).strip()][:500]

    days = src.get("weekendDays")
    if isinstance(days, list):
        clean = sorted({int(d) for d in days if str(d).lstrip("-").isdigit() and 0 <= int(d) <= 6})
    else:
        clean = list(DEFAULT_POLICY["weekendDays"])
    reasons = str_list(src.get("reasons"), DEFAULT_REASONS) or list(DEFAULT_REASONS)
    message = src.get("message")
    return {
        "weekendCheck": src.get("weekendCheck") is not False,
        "weekendDays": clean,
        "devTools": str_list(src.get("devTools"), DEFAULT_DEV_TOOLS),
        "reasons": reasons,
        "message": message.strip()[:2000] if isinstance(message, str) and message.strip() else DEFAULT_POLICY["message"],
    }
