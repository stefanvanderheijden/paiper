"""paiper: review what your AI agent writes, on real pages, and send your comments straight back to it."""
import json as _json

from . import config as _config

__version__ = "0.2.0"


def palette(theme="default"):
    """A theme's colours, for scripts that draw their own charts or illustrations."""
    with open(_config.find_theme(theme) + "/theme.json", encoding="utf-8") as f:
        return _json.load(f).get("palette", {})
