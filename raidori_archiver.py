import html
import json
from pathlib import Path
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set
from bs4 import BeautifulSoup
import httpx

# 日本標準時 (JST = UTC+9)
JST = timezone(timedelta(hours=9))


@dataclass
class CreatorProfile:
    user_id: str
    display_name: str
    handle: str
    avatar_url: Optional[str] = None


@dataclass
class AppConfig:
    cookies: str
    target_users: List[str] = field(default_factory=list)
    max_price: Optional[int] = None
    crawl_interval: float = 2.0
    output_dir: Path = Path("./raidori_archive")
    request_timeout: float = 40.0

    @classmethod
    def load(cls, config_path: str = "config.json") -> "AppConfig":
        path = Path(config_path)
        if not path.is_file():
            raise FileNotFoundError(
                f"Configuration file '{config_path}' not found."
            )

        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        targets = raw.get("target_users", [])
        if isinstance(targets, str):
            targets = [targets]

        raw_cookie = raw.get("cookies", "").strip()
        if raw_cookie.lower().startswith("cookie:"):
            raw_cookie = raw_cookie[7:].strip()

        raw_price = raw.get("max_price")
        max_price = int(raw_price) if raw_price is not None else None

        return cls(
            cookies=raw_cookie,
            target_users=targets,
            max_price=max_price,
            crawl_interval=float(raw.get("crawl_interval", 2.0)),
            output_dir=Path(raw.get("output_dir", "./raidori_archive")),
            request_timeout=float(raw.get("request_timeout", 40.0)),
        )


class RaidoriArchiver:

    def __init__(self, config: AppConfig):
        self.config = config
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Referer": "https://raidori.com/",
            "Origin": "https://raidori.com",
            "Cookie": self.config.cookies,
        }
        self.client = httpx.Client(
            headers=self.headers,
            timeout=self.config.request_timeout,
            follow_redirects=True,
        )

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Executes an HTTP request and strictly enforces a sleep interval afterward."""
        try:
            return self.client.request(method, url, **kwargs)
        finally:
            if self.config.crawl_interval > 0:
                time.sleep(self.config.crawl_interval)

    def _resolve_index(self, data: List[Any], val: Any) -> Any:
        if isinstance(val, int) and 0 <= val < len(data):
            return data[val]
        return val

    @staticmethod
    def _format_jp_timestamp(ts: Any) -> Optional[str]:
        """Unix秒を JST (UTC+9) の 'YYYY年M月D日H時M分' 形式に変換"""
        if isinstance(ts, (int, float)) and ts > 0:
            try:
                dt = datetime.fromtimestamp(ts, tz=JST)
                return f"{dt.year}年{dt.month}月{dt.day}日{dt.hour}時{dt.minute:02d}分"
            except Exception:
                return None
        return None

    @staticmethod
    def _format_comment_timestamp(ts: Any) -> str:
        """コメント投稿日時を JST (UTC+9) の 'YYYY/MM/DD HH:mm' 形式に変換"""
        if isinstance(ts, (int, float)) and ts > 0:
            try:
                dt = datetime.fromtimestamp(ts, tz=JST)
                return dt.strftime("%Y/%m/%d %H:%M")
            except Exception:
                return "不明な日時"
        return "不明な日時"

    def check_session(self) -> Optional[str]:
        if not self.config.cookies:
            print("[-] [Auth Check] config.json の 'cookies' 項目が空です。")
            return None

        try:
            print("[*] Verifying session...")
            resp = self._request("GET", "https://raidori.com/home")

            if "/login" in str(resp.url):
                print(
                    "[-] [Auth Check] セッション無効: ログイン画面へリダイレクトされました"
                    f" ({resp.url})"
                )
                return None

            match = re.search(
                r'class="side-navigation-account-name__name"[^>]*>([^<]+)<',
                resp.text,
            )
            if match:
                return match.group(1).strip()

            soup = BeautifulSoup(resp.text, "html.parser")
            nuxt_script = soup.find("script", id="__NUXT_DATA__")
            if nuxt_script and nuxt_script.string:
                nuxt_data: List[Any] = json.loads(nuxt_script.string)
                for item in nuxt_data:
                    if (
                        isinstance(item, dict)
                        and item.get("status") == "resolved"
                        and "viewer" in item
                    ):
                        viewer = self._resolve_index(nuxt_data, item["viewer"])
                        if isinstance(viewer, dict) and "user" in viewer:
                            user = self._resolve_index(
                                nuxt_data, viewer["user"]
                            )
                            if isinstance(user, dict) and "profile" in user:
                                prof = self._resolve_index(
                                    nuxt_data, user["profile"]
                                )
                                if isinstance(prof, dict) and "name" in prof:
                                    return str(
                                        self._resolve_index(
                                            nuxt_data, prof["name"]
                                        )
                                    )
        except Exception as e:
            print(f"[-] [Auth Check] 通信エラー: {e}")

        return None

    def resolve_creator_profile(self, identifier: str) -> CreatorProfile:
        clean = identifier.lstrip("@")

        graphql_endpoint = "https://api.raidori.com/query"
        query = """
        query ResolveCreator($screenNames: [String!]!) {
          usersByTwitterScreenNames(screenNames: $screenNames) {
            id
            profile {
              name
              twitterScreenName
              iconURL
            }
          }
        }
        """
        try:
            print(f"[*] Resolving target '{clean}' via API...")
            resp = self._request(
                "POST",
                graphql_endpoint,
                json={"query": query, "variables": {"screenNames": [clean]}},
            )
            
            if resp.status_code == 200:
                data = resp.json()
                users = data.get("data", {}).get(
                    "usersByTwitterScreenNames", []
                )
                if users and users[0].get("id"):
                    u = users[0]
                    prof = u.get("profile") or {}
                    return CreatorProfile(
                        user_id=str(u["id"]),
                        display_name=prof.get("name") or clean,
                        handle=prof.get("twitterScreenName") or clean,
                        avatar_url=prof.get("iconURL"),
                    )
        except Exception:
            pass

        print(f"[*] Fallback: Resolving target via HTML...")
        url = (
            f"https://raidori.com/@{clean}"
            if not clean.isdigit()
            else f"https://raidori.com/fanclub/user/{clean}/article"
        )
        resp = self._request("GET", url)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        nuxt_script = soup.find("script", id="__NUXT_DATA__")

        if nuxt_script and nuxt_script.string:
            try:
                nuxt_data: List[Any] = json.loads(nuxt_script.string)
                for item in nuxt_data:
                    if (
                        isinstance(item, dict)
                        and "userId" in item
                        and "userName" in item
                    ):
                        uid = str(
                            self._resolve_index(nuxt_data, item["userId"])
                        )
                        uname = str(
                            self._resolve_index(nuxt_data, item["userName"])
                        )
                        uhandle = str(
                            self._resolve_index(
                                nuxt_data, item.get("handleId", clean)
                            )
                        )
                        uicon = str(
                            self._resolve_index(
                                nuxt_data, item.get("iconUrl", "")
                            )
                        )
                        return CreatorProfile(
                            user_id=uid,
                            display_name=uname,
                            handle=uhandle,
                            avatar_url=uicon or None,
                        )
            except Exception:
                pass

        id_match = re.search(
            r"UserProfileSection:clientInteraction:(\d+)", resp.text
        )
        if not id_match:
            id_match = re.search(r"/fanclub/user/(\d+)/", resp.text)

        if id_match:
            uid = id_match.group(1)
            raw_title = (
                soup.title.string.split("さん")[0].strip()
                if soup.title
                else clean
            )
            return CreatorProfile(
                user_id=uid, display_name=raw_title, handle=clean
            )

        raise ValueError(
            f"クリエイター情報の取得に失敗しました: {identifier}"
        )

    def get_article_list(self, user_id: str) -> List[Dict[str, Any]]:
        graphql_endpoint = "https://api.raidori.com/query"
        query = """
        query GetFanclubArticles($userIds: [ID!]!, $after: String, $first: Uint!) {
          usersByIds(ids: $userIds) {
            publicFanclubArticles(first: $first, after: $after) {
              pageInfo {
                endCursor
                remainingPageCounts
              }
              edges {
                node {
                  id
                  title
                  firstPublishedAt
                  publicScope {
                    __typename
                    ... on FanclubArticlePublicScopeMinimumAmount {
                      type
                      amount
                    }
                  }
                }
              }
            }
          }
        }
        """

        articles: List[Dict[str, Any]] = []
        cursor = None
        page_num = 1

        while True:
            variables = {
                "userIds": [user_id],
                "first": 10
            }
            
            if cursor:
                variables["after"] = cursor
            
            print(f"[*] Fetching page {page_num}...")

            resp = self._request(
                "POST",
                graphql_endpoint,
                json={
                    "operationName": "GetFanclubArticles",
                    "query": query,
                    "variables": variables
                }
            )
            
            if resp.status_code != 200:
                print(f"\n[-] GraphQL API Error Response: {resp.text}\n")
                resp.raise_for_status()

            data = resp.json()
            users = data.get("data", {}).get("usersByIds", [])
            if not users:
                break

            user_data = users[0]
            if not user_data:
                break

            articles_data = user_data.get("publicFanclubArticles", {})
            edges = articles_data.get("edges", [])

            for edge in edges:
                node = edge.get("node", {})
                art_id = node.get("id")
                title = node.get("title", "")
                published_at = node.get("firstPublishedAt", 0)

                price = 0
                scope = node.get("publicScope", {})
                if isinstance(scope, dict):
                    scope_type = scope.get("type")
                    if scope_type == "MinimumAmount":
                        price = scope.get("amount", 0)

                if art_id:
                    articles.append({
                        "id": str(art_id),
                        "title": title,
                        "price": int(price),
                        "published_at": published_at,
                    })

            page_info = articles_data.get("pageInfo", {})
            cursor = page_info.get("endCursor")
            remaining = page_info.get("remainingPageCounts", 0)

            print(f"    -> Extracted {len(edges)} articles. Remaining pages: {remaining}")

            if not cursor or remaining <= 0:
                break
                
            page_num += 1

        return articles

    def render_tiptap_html(self, node: dict) -> str:
        node_type = node.get("type", "")

        if node_type == "text":
            text_val = html.escape(node.get("text", ""))
            marks = node.get("marks", [])
            for mark in marks:
                m_type = mark.get("type")
                if m_type == "bold":
                    text_val = f"<strong>{text_val}</strong>"
                elif m_type == "italic":
                    text_val = f"<em>{text_val}</em>"
                elif m_type == "textStyle":
                    color = mark.get("attrs", {}).get("color")
                    if color:
                        text_val = f'<span style="color: {html.escape(color)}">{text_val}</span>'
            return text_val

        if node_type == "hardBreak":
            return "<br>\n"

        attrs = node.get("attrs", {})
        align = attrs.get("textAlign")
        style_attr = f' style="text-align: {align};"' if align else ""

        content = node.get("content", [])
        inner_html = "".join(self.render_tiptap_html(c) for c in content)

        if node_type == "paragraph":
            return f"<p{style_attr}>{inner_html}</p>\n"
        if node_type == "heading":
            lvl = attrs.get("level", 2)
            clean_inner = re.sub(
                r"^(?:<br>\s*)+|(?:<br>\s*)+$", "", inner_html
            )
            return f"<h{lvl}{style_attr}>{clean_inner}</h{lvl}>\n"
        if node_type == "bulletList":
            return f"<ul>\n{inner_html}</ul>\n"
        if node_type == "orderedList":
            return f"<ol>\n{inner_html}</ol>\n"
        if node_type == "listItem":
            return f"<li>{inner_html}</li>\n"
        if node_type == "blockquote":
            return f"<blockquote>{inner_html}</blockquote>\n"

        return inner_html

    def extract_tiptap_doc(self, data: List[Any]) -> Optional[dict]:
        for item in data:
            if isinstance(item, str) and item.startswith('{"type":"doc"'):
                try:
                    doc = json.loads(item)
                    if doc.get("content"):
                        return doc
                except Exception:
                    continue
        return None

    def extract_comments(self, nuxt_data: List[Any]) -> List[Dict[str, Any]]:
        comments: List[Dict[str, Any]] = []
        for item in nuxt_data:
            if not isinstance(item, dict):
                continue
            tn = self._resolve_index(nuxt_data, item.get("__typename"))
            if tn == "SNSComment":
                c_id = str(self._resolve_index(nuxt_data, item.get("id", "")))
                c_body = str(self._resolve_index(nuxt_data, item.get("body", "")))
                c_ts = self._resolve_index(nuxt_data, item.get("createdAt", 0))

                c_date = self._format_comment_timestamp(c_ts)

                user_name = "ユーザー"
                user_handle = ""

                user_obj = self._resolve_index(nuxt_data, item.get("user"))
                if isinstance(user_obj, dict):
                    prof_obj = self._resolve_index(nuxt_data, user_obj.get("profile"))
                    if isinstance(prof_obj, dict):
                        user_name = str(self._resolve_index(nuxt_data, prof_obj.get("name", "ユーザー")))
                        user_handle = str(self._resolve_index(nuxt_data, prof_obj.get("twitterScreenName", "")))

                # Extract nested replies
                replies = []
                replies_conn = self._resolve_index(nuxt_data, item.get("commentReplies"))
                if isinstance(replies_conn, dict):
                    edges = self._resolve_index(nuxt_data, replies_conn.get("edges"))
                    if isinstance(edges, list):
                        for edge_ref in edges:
                            edge = self._resolve_index(nuxt_data, edge_ref)
                            if isinstance(edge, dict):
                                node = self._resolve_index(nuxt_data, edge.get("node"))
                                if isinstance(node, dict):
                                    r_id = str(self._resolve_index(nuxt_data, node.get("id", "")))
                                    r_body = str(self._resolve_index(nuxt_data, node.get("body", "")))
                                    r_ts = self._resolve_index(nuxt_data, node.get("createdAt", 0))
                                    r_date = self._format_comment_timestamp(r_ts)
                                    
                                    r_user_name = "ユーザー"
                                    r_user_handle = ""
                                    r_user_obj = self._resolve_index(nuxt_data, node.get("user"))
                                    if isinstance(r_user_obj, dict):
                                        r_prof_obj = self._resolve_index(nuxt_data, r_user_obj.get("profile"))
                                        if isinstance(r_prof_obj, dict):
                                            r_user_name = str(self._resolve_index(nuxt_data, r_prof_obj.get("name", "ユーザー")))
                                            r_user_handle = str(self._resolve_index(nuxt_data, r_prof_obj.get("twitterScreenName", "")))
                                            
                                    replies.append({
                                        "id": r_id,
                                        "author": r_user_name,
                                        "handle": r_user_handle,
                                        "body": r_body,
                                        "date": r_date
                                    })

                comments.append({
                    "id": c_id,
                    "author": user_name,
                    "handle": user_handle,
                    "body": c_body,
                    "date": c_date,
                    "replies": replies
                })
        return comments

    def build_article_html(
        self,
        title: str,
        creator: CreatorProfile,
        article_id: str,
        price: int,
        first_pub_str: Optional[str],
        updated_str: Optional[str],
        body_html: str,
        cover_filename: Optional[str],
        media_files: List[str],
        like_count: int,
        comment_count: int,
        comments: List[Dict[str, Any]],
    ) -> str:
        article_url = f"https://raidori.com/fanclub/user/{creator.user_id}/article/{article_id}"

        audio_elements = []
        image_elements = []

        for f in media_files:
            lower = f.lower()
            if lower.endswith((".mp3", ".wav", ".m4a", ".ogg")):
                audio_elements.append(
                    f'<div class="media-card">'
                    f'  <div class="media-label">🎵 音声ファイル ({html.escape(f)})</div>'
                    f'  <audio controls src="./{html.escape(f)}"></audio>'
                    f"</div>"
                )
            elif (
                lower.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
                and f != cover_filename
            ):
                image_elements.append(
                    f'<div class="media-card">'
                    f'  <img src="./{html.escape(f)}" alt="{html.escape(f)}"'
                    ' loading="lazy">'
                    f"</div>"
                )

        cover_html = (
            f'<div class="cover-wrapper">'
            f'  <img src="./{html.escape(cover_filename)}" alt="Cover"'
            ' class="cover-img">'
            f"</div>"
            if cover_filename
            else ""
        )

        media_section = (
            f'<section class="media-section">'
            f'  {"".join(audio_elements)}'
            f'  {"".join(image_elements)}'
            f"</section>"
            if (audio_elements or image_elements)
            else ""
        )

        comments_html_list = []
        for c in comments:
            formatted_body = html.escape(c["body"]).replace("\n", "<br>")
            handle_tag = (
                f' <span class="comment-handle">@{html.escape(c["handle"])}</span>'
                if c["handle"]
                else ""
            )
            host_badge_main = ' <span class="reply-host-badge">クリエイター</span>' if c["handle"] == creator.handle else ""

            # Render nested replies
            replies_html_list = []
            for r in c.get("replies", []):
                r_formatted_body = html.escape(r["body"]).replace("\n", "<br>")
                r_handle_tag = f' <span class="comment-handle">@{html.escape(r["handle"])}</span>' if r["handle"] else ""
                host_badge = ' <span class="reply-host-badge">クリエイター</span>' if r["handle"] == creator.handle else ""
                
                replies_html_list.append(f"""
                <div class="reply-card">
                  <div class="comment-meta">
                    <span class="comment-author">{html.escape(r["author"])}</span>{r_handle_tag}{host_badge}
                    <span class="comment-time">{r["date"]}</span>
                  </div>
                  <div class="comment-body">{r_formatted_body}</div>
                </div>
                """)
                
            replies_section = f'<div class="comment-replies">{"".join(replies_html_list)}</div>' if replies_html_list else ""

            comments_html_list.append(f"""
            <div class="comment-card">
              <div class="comment-meta">
                <span class="comment-author">{html.escape(c["author"])}</span>{handle_tag}{host_badge_main}
                <span class="comment-time">{c["date"]}</span>
              </div>
              <div class="comment-body">{formatted_body}</div>
              {replies_section}
            </div>
            """)

        comments_section = ""
        if comments_html_list:
            comments_section = f"""
            <section class="comments-section">
              <h2 class="comments-heading">コメント ({len(comments)})</h2>
              <div class="comments-list">
                {"".join(comments_html_list)}
              </div>
            </section>
            """

        price_tag_text = f"月額{price:,}円以上公開" if price > 0 else "全体公開"

        date_badges = []
        if first_pub_str:
            date_badges.append(
                f'<span class="date-item">初回公開日時 {html.escape(first_pub_str)}</span>'
            )
        if updated_str:
            date_badges.append(
                f'<span class="date-item">最終更新日時 {html.escape(updated_str)}</span>'
            )
        date_block_html = (
            "".join(date_badges)
            if date_badges
            else (
                '<span class="date-item">初回公開日時'
                f" {datetime.now(JST).strftime('%Y年%m月%d日%H時%M分')}</span>"
            )
        )

        return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background-color: #f7f9fa;
      color: #333333;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", "Noto Sans JP", sans-serif;
      line-height: 1.85;
      -webkit-font-smoothing: antialiased;
      padding: 24px 16px 48px 16px;
    }}
    a {{ color: #0ea8ef; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}

    .reader-container {{ max-width: 760px; margin: 0 auto; }}
    .article-box {{ background: #ffffff; border-radius: 16px; overflow: hidden; box-shadow: 0 2px 12px rgba(0, 0, 0, 0.04); border: 1px solid #edf0f2; }}
    .cover-wrapper {{ width: 100%; aspect-ratio: 16 / 9; background: #e9ecef; overflow: hidden; }}
    .cover-img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
    .article-content {{ padding: 32px 32px 36px 32px; }}

    .creator-bar {{ display: flex; align-items: center; gap: 12px; margin-bottom: 20px; }}
    .creator-avatar {{ width: 44px; height: 44px; border-radius: 50%; object-fit: cover; border: 1px solid #eaeaea; }}
    .creator-name {{ font-size: 15px; font-weight: 700; color: #212529; line-height: 1.3; }}
    .creator-handle {{ font-size: 13px; color: #868e96; line-height: 1.2; }}

    .badge-bar {{ display: flex; gap: 8px; align-items: center; margin-bottom: 16px; flex-wrap: wrap; }}
    .tag-blue {{ display: inline-flex; align-items: center; padding: 2px 10px; border-radius: 6px; background: #f0f7ff; color: #0070f3; font-size: 12px; font-weight: 700; border: 1px solid #d0e6ff; }}
    .tip-orange {{ display: inline-flex; align-items: center; border: 1px solid #ff7a45; color: #ff5722; padding: 2px 8px; border-radius: 6px; font-weight: 700; font-size: 11px; background: #fff; }}

    .article-title {{ font-size: 30px; font-weight: 800; color: #1a1a1a; line-height: 1.35; margin-bottom: 14px; letter-spacing: -0.02em; word-break: break-word; }}
    .article-date-row {{ display: flex; flex-wrap: wrap; gap: 14px; font-size: 12.5px; color: #868e96; margin-bottom: 28px; }}
    
    .wysiwyg-body {{ font-size: 16px; line-height: 2.0; color: #2b2b2b; word-break: break-word; }}
    .wysiwyg-body p {{ margin: 1.2em 0; }}
    .wysiwyg-body p:first-child {{ margin-top: 0; }}
    .wysiwyg-body img {{ max-width: 100%; height: auto; border-radius: 8px; margin: 1em 0; }}

    .media-section {{ margin-top: 36px; padding-top: 24px; border-top: 1px dashed #e9ecef; display: flex; flex-direction: column; gap: 20px; }}
    .media-card {{ background: #f8f9fa; border: 1px solid #e9ecef; border-radius: 12px; padding: 16px; text-align: center; }}
    .media-label {{ font-size: 13px; font-weight: 700; color: #495057; margin-bottom: 8px; }}
    .media-card audio {{ width: 100%; max-width: 500px; }}
    .media-card img {{ max-width: 100%; height: auto; border-radius: 8px; }}

    .reactions-bar {{ display: flex; align-items: center; gap: 18px; margin-top: 32px; padding-top: 18px; border-top: 1px solid #f1f3f5; font-size: 14px; font-weight: 700; color: #495057; }}
    .reaction-item {{ display: flex; align-items: center; gap: 6px; }}
    .reaction-item svg {{ width: 16px; height: 16px; }}
    
    .original-link {{ margin-left: auto; display: flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 600; color: #adb5bd; text-decoration: none; transition: color 0.2s ease; cursor: pointer; }}
    .original-link:hover {{ color: #0070f3; text-decoration: underline; }}
    .original-link svg {{ width: 15px; height: 15px; fill: currentColor; transition: fill 0.2s ease; }}

    .comments-section {{ margin-top: 32px; padding-top: 24px; border-top: 1px solid #edf0f2; }}
    .comments-heading {{ font-size: 16px; font-weight: 800; margin-bottom: 16px; color: #212529; }}
    .comments-list {{ display: flex; flex-direction: column; gap: 12px; }}
    .comment-card {{ padding: 14px 18px; background: #f8f9fa; border: 1px solid #edf0f2; border-radius: 10px; }}
    .comment-meta {{ display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 6px; font-size: 13px; }}
    .comment-author {{ font-weight: 700; color: #212529; }}
    .comment-handle {{ color: #868e96; font-size: 12px; }}
    .comment-time {{ color: #adb5bd; font-size: 11px; margin-left: auto; }}
    .comment-body {{ font-size: 14px; line-height: 1.65; color: #343a40; word-break: break-word; }}
    
    .comment-replies {{ margin-top: 12px; padding-top: 12px; border-top: 1px dashed #dee2e6; display: flex; flex-direction: column; gap: 10px; padding-left: 18px; border-left: 2px solid #e9ecef; margin-left: 4px; }}
    .reply-card {{ padding: 10px 14px; background: #ffffff; border: 1px solid #edf0f2; border-radius: 8px; }}
    .reply-host-badge {{ display: inline-block; margin-left: 6px; padding: 2px 6px; background: #ff7a45; color: #fff; font-size: 10px; border-radius: 4px; font-weight: bold; }}

    @media (max-width: 600px) {{
      .article-content {{ padding: 20px 18px; }}
      .article-title {{ font-size: 24px; }}
    }}
  </style>
</head>
<body>
  <div class="reader-container">
    <article class="article-box">
      {cover_html}
      <div class="article-content">
        <header>
          <div class="creator-bar">
            <img src="./avatar.jpg" alt="{html.escape(creator.display_name)}" class="creator-avatar" onerror="this.style.display='none'">
            <div>
              <div class="creator-name">{html.escape(creator.display_name)}</div>
              <div class="creator-handle">@{html.escape(creator.handle)}</div>
            </div>
          </div>

          <div class="badge-bar">
            <span class="tag-blue">{price_tag_text}</span>
            <span class="tip-orange">見れる</span>
          </div>

          <h1 class="article-title">{html.escape(title)}</h1>
          <div class="article-date-row">
            {date_block_html}
          </div>
        </header>

        <div class="wysiwyg-body">
          {body_html}
        </div>

        {media_section}

        <div class="reactions-bar">
          <div class="reaction-item" title="コメント数">
            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 14 14">
              <path fill="#495057" fill-rule="evenodd" d="M2.65 9.276h2.834l.065 2.087 2.39-2.087h3.594a1.41 1.41 0 0 0 1.47-1.31l-.154-4.122a1.42 1.42 0 0 0-.459-.975 1.4 1.4 0 0 0-1.014-.368H2.467a1.4 1.4 0 0 0-1.013.368 1.42 1.42 0 0 0-.457.942l.16 4.122a1.41 1.41 0 0 0 1.472 1.344zm-2.489-1.3L0 3.801c.03-.639.312-1.24.782-1.67a2.4 2.4 0 0 1 1.73-.629h8.82a2.4 2.4 0 0 1 1.73.629c.47.43.751 1.031.782 1.67L14 7.976a2.42 2.42 0 0 1-.782 1.67 2.4 2.4 0 0 1-1.73.629H8.311L4.618 13.5l-.1-3.225H2.674a2.4 2.4 0 0 1-1.73-.628 2.42 2.42 0 0 1-.782-1.67" clip-rule="evenodd"></path>
            </svg>
            <span>{comment_count}</span>
          </div>
          <div class="reaction-item" title="いいね数">
            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 14 14">
              <path fill="#e03131" fill-rule="evenodd" d="M12.044 3.118 12.02 3.1a2.8 2.8 0 0 0-3.758.268l-.007.007-1.257 1.267-1.256-1.267-.006-.007A2.8 2.8 0 0 0 1.99 3.09a2.793 2.793 0 0 0-.19 4.09l.002.002 5.197 5.257 5.199-5.258a2.79 2.79 0 0 0 .511-3.201 2.8 2.8 0 0 0-.665-.862m.598-.804a3.79 3.79 0 0 1 .268 5.569l-5.291 5.352a.87.87 0 0 1-1.24 0L1.09 7.883a3.793 3.793 0 0 1 .27-5.57 3.8 3.8 0 0 1 5.1.365l.54.544.54-.544a3.8 3.8 0 0 1 2.48-1.168 3.8 3.8 0 0 1 2.62.804z" clip-rule="evenodd"></path>
            </svg>
            <span>{like_count}</span>
          </div>
          
          <a href="{html.escape(article_url)}" target="_blank" rel="noopener noreferrer" class="original-link" title="元記事を開く">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
              <path d="M14 3v2h3.59l-9.83 9.83 1.41 1.41L19 6.41V10h2V3h-7zm-2 16H5V5h7V3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2v-7h-2v7z"/>
            </svg>
            <span>Raidoriで開く</span>
          </a>
        </div>

        {comments_section}
      </div>
    </article>
  </div>
</body>
</html>
"""

    def download_article(
        self,
        creator: CreatorProfile,
        article: Dict[str, Any],
        creator_folder: Path,
    ) -> bool:
        article_id = article["id"]
        title_hint = article["title"]
        price = article["price"]

        url = f"https://raidori.com/fanclub/user/{creator.user_id}/article/{article_id}"
        resp = self._request("GET", url)

        if resp.status_code != 200:
            print(
                f"[-] [HTTP {resp.status_code}] Failed to fetch Article"
                f" {article_id}"
            )
            return False

        soup = BeautifulSoup(resp.text, "html.parser")
        nuxt_script = soup.find("script", id="__NUXT_DATA__")
        raw_html = resp.text

        body_html = ""
        has_content = False
        first_pub_str: Optional[str] = None
        updated_str: Optional[str] = None
        comments: List[Dict[str, Any]] = []

        like_count = 0
        comment_count = 0

        like_el = soup.select_one(
            ".card-controller__like-counter, .favorite-counter"
        )
        if like_el:
            digits = re.sub(r"[^\d]", "", like_el.get_text())
            if digits.isdigit():
                like_count = int(digits)

        comment_el = soup.select_one(
            ".card-controller__comment-counter, .comment-counter"
        )
        if comment_el:
            digits = re.sub(r"[^\d]", "", comment_el.get_text())
            if digits.isdigit():
                comment_count = int(digits)

        if nuxt_script and nuxt_script.string:
            try:
                nuxt_data: List[Any] = json.loads(nuxt_script.string)
                tiptap_doc = self.extract_tiptap_doc(nuxt_data)
                if tiptap_doc:
                    body_html = self.render_tiptap_html(tiptap_doc)
                    has_content = True

                comments = self.extract_comments(nuxt_data)

                if like_count == 0 or comment_count == 0:
                    for item in nuxt_data:
                        if (
                            isinstance(item, dict)
                            and "favoriteCounts" in item
                            and "commentCounts" in item
                        ):
                            fav = item.get("favoriteCounts")
                            com = item.get("commentCounts")
                            if fav is not None:
                                like_count = int(
                                    self._resolve_index(nuxt_data, fav)
                                )
                            if com is not None:
                                comment_count = int(
                                    self._resolve_index(nuxt_data, com)
                                )
                            break

                for item in nuxt_data:
                    if not isinstance(item, dict):
                        continue
                    resolved_item_id = str(
                        self._resolve_index(nuxt_data, item.get("id"))
                    )
                    if (
                        resolved_item_id == str(article_id)
                        and "firstPublishedAt" in item
                    ):
                        pub_ts = self._resolve_index(
                            nuxt_data, item["firstPublishedAt"]
                        )
                        first_pub_str = self._format_jp_timestamp(pub_ts)

                        up_ts = self._resolve_index(
                            nuxt_data, item.get("updatedAt", 0)
                        )
                        updated_str = self._format_jp_timestamp(up_ts)
                        break
            except Exception:
                pass

        if not first_pub_str:
            first_pub_str = datetime.now(JST).strftime("%Y年%m月%d日%H時%M分")

        is_viewable = "見れる" in raw_html
        if not is_viewable and not has_content:
            print(
                f"[-] [Skipped] Article {article_id} ('{title_hint}', {price}"
                " JPY): No viewing permission."
            )
            return False

        raw_title = soup.title.string if soup.title else title_hint
        clean_title = re.sub(
            r'[\\/*?:"<>|.\s]+$', "", re.sub(r'[\\/*?:"<>|]', "", raw_title.split("|")[0].strip())
        )
        if not clean_title:
            clean_title = "Untitled"
            
        folder_name = f"{article_id}_{clean_title}"
        article_dir = creator_folder / folder_name
        article_dir.mkdir(parents=True, exist_ok=True)
        article["folder_name"] = folder_name

        plain_text = re.sub(r"<[^>]+>", "", body_html)
        date_header_txt = f"初回公開日時: {first_pub_str}"
        if updated_str:
            date_header_txt += f"\n最終更新日時: {updated_str}"
        plain_text = f"{title_hint}\n{date_header_txt}\n\n{plain_text}"

        if comments:
            plain_text += f"\n\n{'='*40}\nコメント ({len(comments)}件):\n"
            for c in comments:
                plain_text += f"\n[{c['author']}] {c['date']}\n{c['body']}\n"
                for r in c.get("replies", []):
                    plain_text += f"    ↳ [{r['author']}] {r['date']}\n    {r['body']}\n"
        (article_dir / "article.txt").write_text(plain_text, encoding="utf-8")

        cover_url = None
        og_image = soup.find("meta", property="og:image")
        if og_image and og_image.get("content"):
            cover_url = re.sub(
                r"/cdn-cgi/image/[^/]+/", "/", og_image["content"]
            )

        avatar_img = soup.find("img", class_="account-name__icon")
        avatar_url = (
            avatar_img.get("src") if avatar_img else None
        ) or creator.avatar_url
        if avatar_url:
            avatar_path = article_dir / "avatar.jpg"
            if not avatar_path.exists():
                try:
                    av_res = self._request("GET", avatar_url)
                    if av_res.status_code == 200:
                        avatar_path.write_bytes(av_res.content)
                except Exception:
                    pass

        media_urls: Set[str] = set()
        if cover_url:
            media_urls.add(cover_url)

        viewer_el = soup.find(class_="viewer__main") or soup.find(
            class_="viewer"
        )
        search_html = str(viewer_el) if viewer_el else raw_html

        valid_media_pattern = re.compile(
            r"https?://img\.raidori\.com/[^\"\'\s<>]+\.(?:png|jpg|jpeg|webp|gif|mp3|wav|m4a|ogg|mp4)",
            re.IGNORECASE,
        )

        for match in valid_media_pattern.findall(search_html.replace('\\/', '/')):
            clean_asset = re.sub(r"/cdn-cgi/image/[^/]+/", "/", match)
            if (
                "/icon/" in clean_asset
                or "/defaults/" in clean_asset
                or clean_asset.endswith(".svg")
            ):
                continue
            if (
                "fanclubArticleThumbnail" in clean_asset
                and clean_asset != cover_url
            ):
                continue
            media_urls.add(clean_asset)

        downloaded_media: List[str] = []
        cover_filename = None

        for idx, media_url in enumerate(sorted(media_urls)):
            ext = Path(media_url).suffix or ".png"
            is_cover = media_url == cover_url
            filename = f"cover{ext}" if is_cover else f"attachment_{idx+1}{ext}"
            file_path = article_dir / filename

            if not file_path.exists():
                try:
                    media_resp = self._request("GET", media_url)
                    if media_resp.status_code == 200:
                        file_path.write_bytes(media_resp.content)
                except Exception as e:
                    print(f"    [!] Error downloading {media_url}: {e}")

            if file_path.exists():
                downloaded_media.append(filename)
                if is_cover:
                    cover_filename = filename

        html_content = self.build_article_html(
            title=clean_title,
            creator=creator,
            article_id=article_id,
            price=price,
            first_pub_str=first_pub_str,
            updated_str=updated_str,
            body_html=body_html,
            cover_filename=cover_filename,
            media_files=downloaded_media,
            like_count=like_count,
            comment_count=comment_count,
            comments=comments,
        )
        (article_dir / "article.html").write_text(
            html_content, encoding="utf-8"
        )

        reply_count = sum(len(c.get("replies", [])) for c in comments)
        print(
            f"[+] [Archived] {folder_name} ({price} JPY) -> "
            f"article.html (💬 {comment_count}, ❤️ {like_count}, {len(comments)} comments, {reply_count} replies saved)"
        )
        return True

    def run(self):
        username = self.check_session()
        if username:
            print(f"[+] Authenticated session active as user: {username}")
        else:
            print("[!] WARNING: Could not confirm session authentication.")

        for target in self.config.target_users:
            print(f"\n[*] Resolving target: {target}")
            creator = self.resolve_creator_profile(target)
            print(
                f"[*] Target resolved: {creator.display_name} (@{creator.handle})"
                f" -> User ID: {creator.user_id}"
            )

            creator_folder = (
                self.config.output_dir / f"{creator.handle}_{creator.user_id}"
            )
            creator_folder.mkdir(parents=True, exist_ok=True)

            all_articles = self.get_article_list(creator.user_id)
            print(f"[*] Total genuine articles detected: {len(all_articles)}")

            filtered_articles = []
            for art in all_articles:
                price = art["price"]
                if (
                    self.config.max_price is not None
                    and price > self.config.max_price
                ):
                    print(
                        f"[-] [Price Filter] Skipping {art['id']}"
                        f" ('{art['title']}') - Requires {price} JPY (>"
                        f" {self.config.max_price} JPY)"
                    )
                    continue
                filtered_articles.append(art)

            print(
                f"[*] Target articles within price limit: {len(filtered_articles)}"
            )

            for art in filtered_articles:
                self.download_article(creator, art, creator_folder)

        print("\n[*] Archival process completed.")


if __name__ == "__main__":
    cfg = AppConfig.load("config.json")
    archiver = RaidoriArchiver(cfg)
    archiver.run()