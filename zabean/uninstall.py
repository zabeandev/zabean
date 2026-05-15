"""Entry point for `python -m zabean.uninstall`."""

from zabean.agent.installer import uninstall as _uninstall

if __name__ == "__main__":
    _uninstall()
