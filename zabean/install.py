"""Entry point for `python -m zabean.install`."""

from zabean.agent.installer import install as _install

if __name__ == "__main__":
    _install()
