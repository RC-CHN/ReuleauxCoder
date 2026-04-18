"""Default configuration values."""

from reuleauxcoder.domain.config.schema import DEFAULTS

# Session defaults (actual defaults computed at runtime in paths.py)
# These are fallback values when workspace is not writable
SESSION_DIR_DEFAULT = "~/.rcoder/sessions"
HISTORY_FILE_DEFAULT = "~/.rcoder/history"

# Agent defaults
MAX_CONTEXT_TOKENS_DEFAULT = 128_000
MAX_ROUNDS_DEFAULT = 50

# Context compression defaults
SNIP_THRESHOLD = 0.50  # 50% -> snip tool outputs
SUMMARIZE_THRESHOLD = 0.70  # 70% -> LLM summarize
COLLAPSE_THRESHOLD = 0.90  # 90% -> hard collapse

# Snip strategy defaults
SNIP_KEEP_RECENT_TOOLS = 5
SNIP_THRESHOLD_CHARS = 1500
SNIP_MIN_LINES = 6

# Summarize strategy defaults
SUMMARIZE_KEEP_RECENT_TURNS = 5

# Tool output defaults
TOOL_OUTPUT_SNIP_THRESHOLD = 1500
TOOL_OUTPUT_KEEP_LINES = 3

# Bash tool defaults
BASH_TIMEOUT_DEFAULT = 120
BASH_OUTPUT_TRUNCATE = 15000
BASH_OUTPUT_KEEP_HEAD = 6000
BASH_OUTPUT_KEEP_TAIL = 3000
