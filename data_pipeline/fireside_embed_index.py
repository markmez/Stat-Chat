"""
Fireside semantic search — one-shot embedding index builder.

Pulls the Fireside catalog from Sanity, fetches each episode's WebVTT
transcript (when present), builds an embedding-input string per episode
(title + series + description + truncated transcript), embeds via
OpenAI's text-embedding-3-small, and writes the result to a JSON file.

Output format (consumed by iOS):
    {
        "model": "text-embedding-3-small",
        "dim": 1536,
        "generated_at": "2026-04-28T14:00:00Z",
        "episodes": {
            "<sanity _id>": {
                "title": "...",
                "series": "...",
                "vector": [0.012, -0.034, ...]   // 1536 floats
            },
            ...
        }
    }

Usage:
    cd data_pipeline/
    OPENAI_API_KEY=sk-proj-... python fireside_embed_index.py \
        --output ./embeddings.json

    # Optional: also upload to S3 (requires AWS credentials configured)
    OPENAI_API_KEY=sk-proj-... python fireside_embed_index.py \
        --output ./embeddings.json --upload

This is a manual step — run when Sanity content changes (new episodes,
edited descriptions, etc.). A future scheduled cron can replace this.
"""

import argparse
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request

try:
    from openai import OpenAI
except ImportError:
    print("ERROR: openai not installed. Run: pip install openai", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Sanity GROQ — same project the Fireside iOS app reads from.
# ---------------------------------------------------------------------------

SANITY_PROJECT = "0em2vy4j"
SANITY_DATASET = "production-cms"
SANITY_API_VERSION = "v2025-06-13"
SANITY_API_BASE = (
    f"https://{SANITY_PROJECT}.api.sanity.io/{SANITY_API_VERSION}"
    f"/data/query/{SANITY_DATASET}"
)
# Read-only Sanity token (same as the iOS app uses, baked into the
# SanityClient there).
SANITY_TOKEN = (
    "sk7WXAyTEb6vRrOUqkFFbSVMAUnfeG9J6EMqljWPsEOWmer8zIo0nfr8Qw42KXMijXOybQ5"
    "idPmzqE8n1k84BQpDxFRfSMcDKaoC9y1FAhKB0KTDr2oZnpq9XQdTtgo9lLSbeoYZDGsNeO"
    "HULiLL0koTRFXnySnRCTClSHLTpjgs7TGj5b4A"
)

# Pull title + series + description + chapter titles + transcript URL.
# Mirrors the iOS app's search-corpus query so the embedding text is
# built from the same material the user sees in results.
GROQ_QUERY = """*[_type == "episode"]{
    _id,
    title,
    "descriptionText": pt::text(description),
    "seriesTitle": series->title,
    "hosts": hosts[]->name,
    "guests": guests[]->name,
    "chapters": chapters[]{title},
    "transcriptURL": select(
      defined(transcriptFileURI) =>
        "https://dzvnta9wbxyyv.cloudfront.net/" + string::split(transcriptFileURI, "s3://fireside-poc-827207864714/")[1],
      null
    )
}"""


# ---------------------------------------------------------------------------
# OpenAI embeddings.
# text-embedding-3-small: 1536 dims, ~$0.02/M tokens. 8192-token input cap.
# Truncate transcript so combined input stays well under the cap (we aim
# for ~5000 tokens of transcript max ~= ~20KB of text).
# ---------------------------------------------------------------------------

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
TRANSCRIPT_CHAR_BUDGET = 18_000  # ~4500 tokens, leaves room for metadata


def fetch_sanity_episodes() -> list[dict]:
    encoded = urllib.parse.quote(GROQ_QUERY)
    url = f"{SANITY_API_BASE}?query={encoded}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {SANITY_TOKEN}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read())
    return body.get("result", [])


def fetch_vtt_text(json_url: str) -> str:
    """Convert a transcript JSON URL to its VTT counterpart and return the
    concatenated cue text. Empty string on failure (the embedding still
    runs without transcript content)."""
    if not json_url or not json_url.endswith(".json"):
        return ""
    vtt_url = json_url[:-5] + ".vtt"
    try:
        with urllib.request.urlopen(vtt_url, timeout=20) as resp:
            content = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"  warn: failed to fetch VTT for {json_url}: {e}", file=sys.stderr)
        return ""

    # Strip WebVTT structural lines, keep cue text only.
    cue_lines: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "WEBVTT" or stripped.startswith("NOTE"):
            continue
        if "-->" in stripped:
            continue
        if stripped.isdigit():
            continue
        cue_lines.append(stripped)

    return " ".join(cue_lines)


def build_embedding_text(ep: dict, transcript: str) -> str:
    """Assemble the string sent to the embedder. Order matters less than
    inclusion — modern transformer embeddings weight the whole input,
    so we put descriptive metadata first and append the transcript
    until the char budget is hit."""
    parts: list[str] = []
    if title := ep.get("title"):
        parts.append(f"Title: {title}")
    if series := ep.get("seriesTitle"):
        parts.append(f"Series: {series}")
    if hosts := ep.get("hosts") or []:
        parts.append(f"Hosts: {', '.join(hosts)}")
    if guests := ep.get("guests") or []:
        parts.append(f"Guests: {', '.join(guests)}")
    if desc := ep.get("descriptionText"):
        parts.append(f"Description: {desc}")
    chapters = ep.get("chapters") or []
    chapter_titles = [c.get("title", "") for c in chapters if c.get("title")]
    if chapter_titles:
        parts.append("Chapters: " + " | ".join(chapter_titles))
    if transcript:
        # Truncate to char budget. Find a word boundary to avoid
        # cutting mid-word (cleaner input for the embedder).
        if len(transcript) > TRANSCRIPT_CHAR_BUDGET:
            cut = transcript[:TRANSCRIPT_CHAR_BUDGET]
            last_space = cut.rfind(" ")
            if last_space > 0:
                cut = cut[:last_space]
            transcript = cut
        parts.append(f"Transcript: {transcript}")
    return "\n\n".join(parts)


def embed(client: OpenAI, text: str) -> list[float]:
    resp = client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return resp.data[0].embedding


def upload_to_s3(local_path: str) -> None:
    """Upload via aws CLI to s3://fireside-poc-827207864714/search/embeddings.json
    so it's reachable at https://dzvnta9wbxyyv.cloudfront.net/search/embeddings.json
    (same CloudFront the transcripts are served from)."""
    import subprocess
    s3_key = "s3://fireside-poc-827207864714/search/embeddings.json"
    print(f"\nUploading {local_path} → {s3_key}")
    cmd = [
        "aws", "s3", "cp", local_path, s3_key,
        "--content-type", "application/json",
        "--cache-control", "max-age=300",  # CloudFront caches 5 min
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"S3 upload failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    print("✅ Uploaded.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Fireside embedding index")
    parser.add_argument("--output", default="./embeddings.json", help="Output JSON path")
    parser.add_argument("--upload", action="store_true", help="Upload to S3 after writing")
    args = parser.parse_args()

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    client = OpenAI(api_key=api_key)

    print("Fetching Sanity catalog…")
    episodes = fetch_sanity_episodes()
    print(f"  {len(episodes)} episodes")

    index: dict = {}
    for i, ep in enumerate(episodes, 1):
        ep_id = ep.get("_id")
        title = ep.get("title", "(untitled)")
        if not ep_id:
            print(f"  [{i}/{len(episodes)}] skip — no _id: {title}")
            continue

        print(f"  [{i}/{len(episodes)}] {title}")

        transcript_url = ep.get("transcriptURL")
        transcript = fetch_vtt_text(transcript_url) if transcript_url else ""
        text = build_embedding_text(ep, transcript)

        try:
            vector = embed(client, text)
        except Exception as e:
            print(f"    embed failed: {e}", file=sys.stderr)
            continue

        index[ep_id] = {
            "title": title,
            "series": ep.get("seriesTitle") or "",
            "vector": vector,
        }

    output = {
        "model": EMBEDDING_MODEL,
        "dim": EMBEDDING_DIM,
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "episodes": index,
    }

    with open(args.output, "w") as f:
        json.dump(output, f)
    bytes_written = os.path.getsize(args.output)
    print(f"\n✅ Wrote {len(index)} episodes ({bytes_written / 1024:.1f} KB) → {args.output}")

    if args.upload:
        upload_to_s3(args.output)


if __name__ == "__main__":
    main()
