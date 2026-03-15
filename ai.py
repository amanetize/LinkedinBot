"""
ai.py — AI Content Generator

Uses Tavily for web search and Groq Llama (llama-3.3-70b-versatile) for all generation.
No compound model; web context is fetched via Tavily and passed to the LLM.
"""

import os
from groq import Groq
from tavily import TavilyClient
from dotenv import load_dotenv

load_dotenv()
client = Groq(api_key=os.environ["GROQ_API_KEY"])
_tavily_client = None

def _get_tavily():
    global _tavily_client
    if _tavily_client is None and os.environ.get("TAVILY_API_KEY"):
        _tavily_client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    return _tavily_client

EVAL_MODEL = "llama-3.3-70b-versatile"


def _tavily_search(query: str, topic: str = "general", max_results: int = 5) -> str:
    """Run Tavily search and return a plain-text context block for the LLM."""
    out = _tavily_search_with_answer(query, topic=topic, max_results=max_results, include_answer=False)
    return out["search_context"]


def _tavily_search_with_answer(
    query: str,
    topic: str = "general",
    max_results: int = 5,
    include_answer: bool = True,
    time_range: str = None,
) -> dict:
    """
    Run Tavily search. Returns {"answer": str or "", "search_context": str}.
    When include_answer=True, Tavily returns an LLM-phrased answer we use as the default draft.
    time_range: optional "day", "week", "month", "year" (or "d","w","m","y").
    """
    tc = _get_tavily()
    if tc is None:
        return {"answer": "", "search_context": ""}
    try:
        kwargs = dict(query=query, topic=topic, max_results=max_results, include_answer=include_answer)
        if time_range:
            kwargs["time_range"] = time_range

        response = tc.search(**kwargs)

    except TypeError as e:
        # Some Tavily versions may not support the time_range argument, so retry without it.
        if "time_range" in str(e):
            try:
                kwargs.pop("time_range", None)
                response = tc.search(**kwargs)
            except Exception as e2:
                print(f"[ai] Tavily search retry without time_range failed: {e2}")
                return {"answer": "", "search_context": ""}
        else:
            print(f"[ai] Tavily search TypeError: {e}")
            return {"answer": "", "search_context": ""}
    except Exception as e:
        print(f"[ai] Tavily search error: {e}")
        return {"answer": "", "search_context": ""}

    try:
        results = response.get("results") or []
        answer = (response.get("answer") or "").strip() if include_answer else ""

        lines = []
        for r in results:
            title = r.get("title", "")
            content = r.get("content", "")
            url = r.get("url", "")
            if content:
                lines.append(f"- {title}\n  {content[:600]}\n  Source: {url}")
        search_context = "\n\n".join(lines) if lines else ""
        return {"answer": answer, "search_context": search_context}
    except Exception as e:
        print(f"[ai] Tavily search parse error: {e}")
        return {"answer": "", "search_context": ""}


def generate_comment(post_text: str, author_title: str, existing_comments: list = None) -> str:
    """Generate a short, human-like LinkedIn comment. Uses existing_comments (scraped from the post) to avoid repeating others and match tone."""
    comments_ctx = ""
    if existing_comments:
        samples = existing_comments[:10]
        comments_ctx = (
            "\nOther people already commented this (DO NOT repeat or echo any of these):\n"
            + "\n".join(f"- {c[:150]}" for c in samples)
            + "\n"
        )

    web_ctx = _tavily_search("AI data science trends 2025", topic="general", max_results=3)
    web_block = ""
    if web_ctx:
        web_block = "\nOptional context from web (use only if relevant):\n" + web_ctx + "\n"

    prompt = f"""You are a Data Scientist leaving a short, specific, genuine comment on a LinkedIn post.

STRICT RULES:
- Write ONLY the comment text. Nothing else. No labels, no quotes, no preamble.
- MAX 2 sentences. Short is better.
- You MUST reference something SPECIFIC from the post — a concrete detail, example, number, analogy, or idea the author actually mentioned. Generic comments are forbidden.
- Sound like a real person, not a bot. Warm, natural, casual.
- NO hollow openers: never start with "Great", "Love", "Amazing", "Interesting", "This is", "I love", "What a", "Wow", or similar filler words.
- NO hyphens, dashes, or em dashes.
- NO questions. Make a statement or share a thought.
- Zero emojis unless it feels completely natural (max 1).
- NEVER mention job-seeking, hiring, or self-promotion.
- The comment should feel like something a thoughtful colleague would text after reading the post.
{web_block}
TONE:
- If technical post: add a brief specific insight or observation about one thing mentioned.
- If personal/story post: acknowledge one specific moment or detail the author shared.
- If learning/building post: be genuinely encouraging about the specific thing they built or learned.
- Match energy of existing comments if present.
{comments_ctx}
Post by {author_title}:
\"\"\"{post_text[:1500]}\"\"\"

Write a specific, genuine 1-2 sentence comment that references a concrete detail from the post above:"""

    try:
        response = client.chat.completions.create(
            model=EVAL_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.85,
        )
        comment = response.choices[0].message.content.strip()
        if comment.startswith('"') and comment.endswith('"'):
            comment = comment[1:-1]
        if comment.startswith("'") and comment.endswith("'"):
            comment = comment[1:-1]
        return comment
    except Exception as e:
        print(f"[ai] Comment generation error: {e}")
        return ""


def generate_comment_rephrase_with_instruction(current_draft: str, user_instruction: str) -> str:
    """Rephrase the comment following the user's instruction (e.g. 'make it shorter', 'more formal')."""
    prompt = f"""You have a LinkedIn comment draft. The user wants you to rephrase it according to their instruction.

Current comment:
{current_draft}

User instruction: {user_instruction}

Output ONLY the new comment text, nothing else. Keep it short (2-3 lines). No quotes, no labels."""

    try:
        response = client.chat.completions.create(
            model=EVAL_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
        )
        comment = response.choices[0].message.content.strip()
        if comment.startswith('"') and comment.endswith('"'):
            comment = comment[1:-1]
        if comment.startswith("'") and comment.endswith("'"):
            comment = comment[1:-1]
        return comment
    except Exception as e:
        print(f"[ai] Comment rephrase error: {e}")
        return ""


def _draft_news_post_from_context(search_ctx: str) -> str:
    """Use Llama with search context to draft a LinkedIn post: exactly 5 bullet points (top 5 news items)."""
    prompt = f"""Use the following search results about AI news from the past week.

Search results:
{search_ctx}

Write a short LinkedIn post with exactly 5 bullet points. Pick the TOP 5 most interesting/important news items from the results — one bullet per news story (not 5 takeaways from one story).

RULES:
- Write ONLY the post content, nothing else.
- Exactly 5 bullet points. Start each line with • or - and one news item per line (one sentence each).
- Include every noun like numbers and names to keep it information rich.
- Sound like a real person sharing the week's top AI news. Not a journalist.
- Simple, conversational English. Simple punctuation only.
- NO hyphens or dashes in the middle of sentences (only for bullet markers).
- Only 1-2 emojis total if any, placed naturally. Zero is fine.
- After the last bullet, add a blank line then exactly 3 hashtags on the last line: #ai plus 2 others relevant to the topic.
- Do NOT write a paragraph; output must be exactly 5 bullet points.

Write the post (5 bullet points):"""

    try:
        response = client.chat.completions.create(
            model=EVAL_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8,
        )
        post = response.choices[0].message.content.strip()
        if post.startswith('"') and post.endswith('"'):
            post = post[1:-1]
        return post
    except Exception as e:
        print(f"[ai] News post (from context) error: {e}")
        return ""


def generate_news_post() -> dict:
    """
    Fetch top 5 AI news from the past week via Tavily, then Llama drafts a post with 5 bullet points.
    Returns {"content": str, "search_context": str}.
    """
    out = _tavily_search_with_answer(
        "top 5 AI news past week",
        topic="news",
        max_results=15,
        include_answer=False,
        time_range="week",
    )
    search_ctx = out.get("search_context", "")

    if not search_ctx:
        search_ctx = "No recent results. Use general knowledge about top AI news from the past week (e.g. OpenAI, Google, startups, research)."

    content = _draft_news_post_from_context(search_ctx)
    return {"content": content, "search_context": search_ctx}


def generate_news_post_rephrase(search_context: str) -> str:
    """Rephrase a news post using the same search context (Llama only)."""
    return _draft_news_post_from_context(search_context)


def generate_news_post_rephrase_with_instruction(
    search_context: str, current_content: str, user_instruction: str
) -> str:
    """Rephrase the news post following the user's instruction (e.g. 'make it more casual', 'focus on funding only')."""
    prompt = f"""You have a LinkedIn post draft and the user wants you to rephrase it according to their instruction.

Current draft:
{current_content}

Search context (for reference):
{search_context[:4000]}

User instruction: {user_instruction}

Rephrase the post following the user's instruction. Keep the same format (bullet points, then blank line, then 3 hashtags). Write ONLY the new post content, nothing else."""

    try:
        response = client.chat.completions.create(
            model=EVAL_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
        )
        post = response.choices[0].message.content.strip()
        if post.startswith('"') and post.endswith('"'):
            post = post[1:-1]
        return post
    except Exception as e:
        print(f"[ai] News rephrase with instruction error: {e}")
        return ""