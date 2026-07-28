#!/usr/bin/env python3
"""Print the 100/500/1000-cell CLI transcript benchmark as JSON."""

import json

from reuleauxcoder.interfaces.tui.transcript_benchmark import benchmark_transcript


if __name__ == "__main__":
    print(json.dumps(benchmark_transcript(), indent=2, ensure_ascii=False))
