"""Stand-in stdio MCP server for tests/test_social_mcp.py.

Mirrors the tool names, arguments and output shapes of the real servers
(@mikusnuz/meta-mcp for Instagram, pauling-ai/youtube-mcp-server for
YouTube) with canned data, so lotus_mcp / lotus_social can be exercised
without credentials. Reply calls are appended to $FAKE_SOCIAL_LOG.

    python tests/fake_social_mcp.py instagram|youtube
"""

import json
import os
import sys

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

PLATFORM = sys.argv[1] if len(sys.argv) > 1 else "instagram"
LOG = os.environ.get("FAKE_SOCIAL_LOG")
server = MCPServer(f"fake-{PLATFORM}")


def _log(entry):
    if LOG:
        with open(LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")


if PLATFORM == "instagram":
    @server.tool()
    def ig_get_profile() -> dict:
        return {"id": "178", "username": "techengine.lab", "followers_count": 5120,
                "media_count": 2}

    @server.tool()
    def ig_get_media_list(limit: int = 25, after: str = "") -> dict:
        return {"data": [
            {"id": "m1", "caption": "5 Mac shortcuts nobody uses #productivity",
             "media_type": "CAROUSEL_ALBUM", "like_count": 340, "comments_count": 4},
            {"id": "m2", "caption": "Reel: M1 Max vs M4 render test",
             "media_type": "VIDEO", "like_count": 90, "comments_count": 1},
        ][:limit]}

    @server.tool()
    def ig_get_comments(media_id: str, limit: int = 25, after: str = "") -> dict:
        data = {
            "m1": [
                {"id": "c1", "username": "riya.codes", "text": "Which shortcut is on slide 3?",
                 "timestamp": "2026-10-11T10:00:00+0000"},
                {"id": "c2", "username": "follow4follow_bot",
                 "text": "DM us to go viral!! cheap followers link in bio",
                 "timestamp": "2026-10-11T11:00:00+0000"},
                {"id": "c3", "username": "arjun", "text": "Saved, thanks!",
                 "timestamp": "2026-10-11T12:00:00+0000",
                 "replies": {"data": [{"id": "r1", "username": "techengine.lab",
                                       "text": "Glad it helps!"}]}},
                {"id": "c4", "username": "techengine.lab", "text": "Part 2 tomorrow",
                 "timestamp": "2026-10-11T13:00:00+0000"},
            ],
            "m2": [
                {"id": "c5", "username": "meera", "text": "kya ye 4K export tha?",
                 "timestamp": "2026-10-12T09:00:00+0000"},
            ],
        }
        return {"data": data.get(media_id, [])}

    @server.tool()
    def ig_reply_to_comment(comment_id: str, message: str) -> dict:
        _log({"tool": "ig_reply_to_comment", "comment_id": comment_id, "message": message})
        return {"id": f"reply_{comment_id}"}

    @server.tool()
    def ig_get_media_insights(media_id: str, metric: str = "") -> dict:
        return {"data": [{"name": "reach", "values": [{"value": 4100 if media_id == "m1" else 800}]}]}

    @server.tool()
    def ig_get_account_insights(metric: str, period: str, metric_type: str = "",
                                breakdown: str = "", timeframe: str = "",
                                since: str = "", until: str = "") -> dict:
        return {"data": [{"name": "reach", "total_value": {"value": 21000}}]}

    @server.tool()
    def ig_fail() -> dict:
        raise ToolError("Graph API error 190: token expired")

else:
    @server.tool()
    def youtube_get_channel(channel_id: str = "", handle: str = "", mine: bool = False) -> dict:
        return {"id": "UC1", "title": "Techengine", "subscribers": 812, "video_count": 2}

    @server.tool()
    def youtube_list_videos(channel_id: str = "", playlist_id: str = "",
                            mine: bool = False, max_results: int = 20) -> dict:
        return {"videos": [{"id": "v1", "title": "M1 Max in 2026", "views": 1200,
                            "comment_count": 2}], "total": 1}

    @server.tool()
    def youtube_list_comments(video_id: str, max_results: int = 20,
                              order: str = "relevance") -> dict:
        return {"video_id": video_id, "total": 2, "comments": [
            {"comment_id": "y1", "thread_id": "t1", "author": "@sam",
             "text": "Great video!<br>Can you test &quot;Final Cut&quot; next?",
             "likes": 3, "published_at": "2026-10-10T08:00:00Z", "reply_count": 0},
            {"comment_id": "y2", "thread_id": "t2", "author": "@lee",
             "text": "Already answered", "likes": 0,
             "published_at": "2026-10-09T08:00:00Z", "reply_count": 1},
        ]}

    @server.tool()
    def youtube_reply_to_comment(parent_id: str, text: str) -> dict:
        _log({"tool": "youtube_reply_to_comment", "parent_id": parent_id, "text": text})
        return {"reply_id": f"yr_{parent_id}", "parent_id": parent_id,
                "text": text, "posted": True}

    @server.tool()
    def youtube_analytics_overview(start_date: str = "", end_date: str = "") -> dict:
        return {"rows": [[5400, 9100, 101, 40, 3, 300, 22, 9]]}

    @server.tool()
    def youtube_analytics_top_videos(start_date: str = "", end_date: str = "",
                                     max_results: int = 20) -> dict:
        return {"rows": [["v1", 1200]]}


if __name__ == "__main__":
    server.run()
