"""
Refresh the daily team context cache used by src/py/nba_stats.py.
"""

import json
import sys

from team_context import TEAM_CONTEXT_FILE, build_team_context_cache, save_team_context_cache


def main():
    cache = build_team_context_cache()
    save_team_context_cache(cache, TEAM_CONTEXT_FILE)
    print(
        json.dumps(
            {
                "success": True,
                "generatedAt": cache["generatedAt"],
                "season": cache["season"],
                "teamCount": len(cache.get("teams", {})),
                "path": TEAM_CONTEXT_FILE,
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}))
        sys.exit(1)
