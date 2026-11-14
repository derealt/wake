"""Places where a file makes something run later, without the agent.

When an agent is stopped, WAKE removes every file the agent made in these places and puts
back your version of every one of your files it changed here. The list follows the Linux
persistence locations catalogued by MITRE ATT&CK (shell configuration, scheduled jobs,
services, autostart entries, SSH keys, git hooks, Python start-up files, the dynamic linker).
"""
import re

PATTERNS = [
    r"/\.(bashrc|bash_profile|bash_login|bash_logout|profile|zshrc|zprofile|zlogin|zlogout|zshenv|kshrc|mkshrc|cshrc|tcshrc|login|xprofile|xinitrc|xsession|xsessionrc)$",
    r"/\.config/(fish|autostart|systemd|environment\.d)/",
    r"/\.local/share/systemd/",
    r"^/etc/(profile|bash\.bashrc|zsh/.*|environment|crontab|anacrontab|rc\.local|ld\.so\.preload)$",
    r"^/etc/(profile\.d|cron\.[a-z]+|systemd|init\.d|rc\d\.d|xdg/autostart|udev/rules\.d|sudoers\.d)/",
    r"/var/spool/(cron|at|anacron)/",
    r"/\.ssh/(authorized_keys2?|rc|environment)$",
    r"/\.git/hooks/[^/]+$",
    r"/\.(gitconfig|githooks/.+)$",
    r"/\.config/git/config$",
    r"/site-packages/[^/]+\.pth$",
    r"/(site|user)customize\.py$",
    r"/\.(envrc|pam_environment)$",
    r"/Library/Launch(Agents|Daemons)/",
]
_RX = re.compile("|".join(f"(?:{p})" for p in PATTERNS))


def is_trigger(path):
    return bool(path) and bool(_RX.search(path))
