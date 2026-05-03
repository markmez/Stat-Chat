"""
Fireside semantic search — embedding index builder.

Pulls the Fireside catalog from Sanity, fetches each episode's WebVTT
transcript (when present), and produces THREE levels of embeddings:

  - Episode-level (title + series + description + truncated transcript)
    and chapter-level (chapter title + transcript within chapter range)
    bundled into a single `embeddings.json` for app-wide search.
  - Per-episode CUE-level embeddings, one JSON file per episode, used
    for in-episode search ("find the moment where they said X"). Each
    VTT cue gets its own 1536d vector — time codes stay precise and
    iOS can deep-link to the exact cue start.

App-wide bundle format (consumed by iOS):
    {
        "model": "text-embedding-3-small",
        "dim": 1536,
        "generated_at": "2026-04-28T14:00:00Z",
        "episodes": {"<sanity _id>": {"title": "...", "series": "...", "vector": [...]}},
        "chapters": [{"episode_id": "...", "chapter_number": 1, "title": "...", "vector": [...]}]
    }

Per-episode cue file format (one file per episode):
    {
        "model": "text-embedding-3-small",
        "dim": 1536,
        "episode_id": "...",
        "generated_at": "...",
        "cue_count": 750,
        "cues": [
            {"start": 12.5, "end": 15.3, "text": "...", "vector": [...]}
        ]
    }

Usage:
    cd data_pipeline/
    OPENAI_API_KEY=sk-proj-... python fireside_embed_index.py \
        --output ./embeddings.json --episode-output-dir ./episode-embeddings

    # Optional: also upload everything to S3
    OPENAI_API_KEY=sk-proj-... python fireside_embed_index.py \
        --output ./embeddings.json --episode-output-dir ./episode-embeddings --upload

    # Skip per-episode cue embeddings (chapter-level only, faster):
    OPENAI_API_KEY=sk-proj-... python fireside_embed_index.py \
        --output ./embeddings.json --skip-cues

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
from typing import List, Optional, Tuple

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

# Pull title + series + description + chapters (with timecodes) +
# transcript URL. Chapter timecodes let us slice the VTT cues into
# per-chapter chunks for chapter-level embeddings.
GROQ_QUERY = """*[_type == "episode"]{
    _id,
    title,
    "descriptionText": pt::text(description),
    "seriesTitle": series->title,
    "hosts": hosts[]->name,
    "guests": guests[]->name,
    "chapters": chapters[]{chapterNumber, title, timeCodeIn, timeCodeOut},
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


def fetch_sanity_episodes() -> List[dict]:
    encoded = urllib.parse.quote(GROQ_QUERY)
    url = f"{SANITY_API_BASE}?query={encoded}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {SANITY_TOKEN}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read())
    return body.get("result", [])


def parse_timecode(s: Optional[str]) -> Optional[float]:
    """Parse 'HH:MM:SS.mmm' / 'MM:SS.mmm' / '12.34' to seconds. None on
    failure. Mirrors the iOS-side parser."""
    if not s:
        return None
    parts = s.split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return None
    return None


def fetch_vtt_cues(json_url: str) -> List[Tuple[float, float, str]]:
    """Convert a transcript JSON URL to its VTT counterpart and return a
    list of (start_seconds, end_seconds, text) cues. Empty list on
    failure."""
    if not json_url or not json_url.endswith(".json"):
        return []
    vtt_url = json_url[:-5] + ".vtt"
    try:
        with urllib.request.urlopen(vtt_url, timeout=20) as resp:
            content = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"  warn: failed to fetch VTT for {json_url}: {e}", file=sys.stderr)
        return []

    cues: List[Tuple[float, float, str]] = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if "-->" in line:
            parts = line.split(" --> ")
            if len(parts) == 2:
                start = parse_timecode(parts[0])
                end_part = parts[1].split(" ")[0]  # strip optional cue settings
                end = parse_timecode(end_part)
                if start is not None and end is not None:
                    text_parts: List[str] = []
                    i += 1
                    while i < len(lines) and lines[i].strip():
                        text_parts.append(lines[i].strip())
                        i += 1
                    if text_parts:
                        cues.append((start, end, " ".join(text_parts)))
        i += 1
    return cues


def cues_concatenated(cues: List[Tuple[float, float, str]]) -> str:
    return " ".join(text for _, _, text in cues)


def cues_within_range(
    cues: List[Tuple[float, float, str]],
    start: float,
    end: float,
) -> str:
    """Concatenate cue text whose start_time falls in [start, end). The
    chapter resolver uses the same logic; consistency keeps semantic
    matches aligned with where the iOS app would deep-link."""
    return " ".join(text for s, _, text in cues if start <= s < end)


def build_episode_embedding_text(ep: dict, transcript: str) -> str:
    """Assemble the string sent to the embedder. Order matters less than
    inclusion — modern transformer embeddings weight the whole input,
    so we put descriptive metadata first and append the transcript
    until the char budget is hit."""
    parts: List[str] = []
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
        if len(transcript) > TRANSCRIPT_CHAR_BUDGET:
            cut = transcript[:TRANSCRIPT_CHAR_BUDGET]
            last_space = cut.rfind(" ")
            if last_space > 0:
                cut = cut[:last_space]
            transcript = cut
        parts.append(f"Transcript: {transcript}")
    return "\n\n".join(parts)


# Chapter embedding inputs are usually short (chapter title + a few
# minutes of transcript), so we use a smaller budget per chapter to
# keep average tokens reasonable. Long chapters still get plenty.
CHAPTER_CHAR_BUDGET = 8_000


def build_chapter_embedding_text(ep: dict, chapter: dict, chapter_transcript: str) -> str:
    parts: List[str] = []
    if title := ep.get("title"):
        parts.append(f"Episode: {title}")
    if series := ep.get("seriesTitle"):
        parts.append(f"Series: {series}")
    chapter_title = chapter.get("title") or ""
    chapter_num = chapter.get("chapterNumber")
    if chapter_num is not None:
        parts.append(f"Chapter {chapter_num}: {chapter_title}")
    elif chapter_title:
        parts.append(f"Chapter: {chapter_title}")
    if chapter_transcript:
        if len(chapter_transcript) > CHAPTER_CHAR_BUDGET:
            cut = chapter_transcript[:CHAPTER_CHAR_BUDGET]
            last_space = cut.rfind(" ")
            if last_space > 0:
                cut = cut[:last_space]
            chapter_transcript = cut
        parts.append(f"Transcript: {chapter_transcript}")
    return "\n\n".join(parts)


def embed(client: OpenAI, text: str) -> List[float]:
    resp = client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return resp.data[0].embedding


# OpenAI's embedding endpoint accepts up to 2048 inputs per request and up
# to 8191 tokens per input. Cues are tiny (~12 words / ~17 tokens), so
# batching ~500 per call keeps us well under the token budget AND the
# per-request size limit while drastically cutting round trips. ~750
# cues per episode → 2 calls per episode instead of 750.
CUE_BATCH_SIZE = 500


def embed_batch(client: OpenAI, texts: List[str]) -> List[List[float]]:
    """Embed a batch of texts in a single API call. Preserves input
    ordering (OpenAI returns embeddings in the same order)."""
    if not texts:
        return []
    resp = client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
    # The SDK returns data sorted by index already, but be explicit.
    sorted_data = sorted(resp.data, key=lambda d: d.index)
    return [d.embedding for d in sorted_data]


def embed_cues_for_episode(
    client: OpenAI,
    cues: List[Tuple[float, float, str]],
) -> List[dict]:
    """Embed every cue in the episode using batch requests. Returns a
    list of dicts ready for serialization. Cues with empty text are
    skipped — embeddings on empty strings waste tokens and produce
    near-meaningless vectors that pollute search."""
    cleaned: List[Tuple[float, float, str]] = [
        (s, e, t.strip()) for s, e, t in cues if t and t.strip()
    ]
    if not cleaned:
        return []

    out: List[dict] = []
    for i in range(0, len(cleaned), CUE_BATCH_SIZE):
        batch = cleaned[i : i + CUE_BATCH_SIZE]
        texts = [t for _, _, t in batch]
        try:
            vectors = embed_batch(client, texts)
        except Exception as e:
            print(f"    cue batch {i // CUE_BATCH_SIZE} embed failed: {e}", file=sys.stderr)
            # On a failed batch, skip this slice rather than aborting the
            # whole episode — partial coverage is better than none.
            continue
        for (start, end, text), vector in zip(batch, vectors):
            out.append({
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
                "vector": vector,
            })
    return out


S3_BUCKET = "fireside-poc-827207864714"
S3_BUNDLE_KEY = f"s3://{S3_BUCKET}/search/embeddings.json"
S3_EPISODE_PREFIX = f"s3://{S3_BUCKET}/search/episode-embeddings/"


def upload_to_s3(local_path: str) -> None:
    """Upload the chapter+episode bundle to s3://.../search/embeddings.json,
    reachable at https://dzvnta9wbxyyv.cloudfront.net/search/embeddings.json."""
    import subprocess
    print(f"\nUploading {local_path} → {S3_BUNDLE_KEY}")
    cmd = [
        "aws", "s3", "cp", local_path, S3_BUNDLE_KEY,
        "--content-type", "application/json",
        "--cache-control", "max-age=300",  # CloudFront caches 5 min
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"S3 upload failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    print("✅ Uploaded bundle.")


def upload_episode_embeddings_to_s3(local_dir: str) -> None:
    """Sync per-episode cue embedding files to s3://.../search/episode-embeddings/.
    Each becomes reachable at https://dzvnta9wbxyyv.cloudfront.net/search/
    episode-embeddings/{episodeId}.json. Uses `aws s3 sync` so unchanged
    files don't re-upload on subsequent runs."""
    import subprocess
    print(f"\nSyncing {local_dir} → {S3_EPISODE_PREFIX}")
    cmd = [
        "aws", "s3", "sync", local_dir, S3_EPISODE_PREFIX,
        "--content-type", "application/json",
        "--cache-control", "max-age=300",
        "--exclude", ".*",  # skip dotfiles
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"S3 episode sync failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    print("✅ Synced episode embeddings.")


def write_episode_cue_file(
    episode_id: str,
    cues_with_vectors: List[dict],
    output_dir: str,
) -> str:
    """Write a per-episode cue-embedding JSON to disk. Filename is the
    Sanity _id (URL-safe by convention). Returns the local path."""
    os.makedirs(output_dir, exist_ok=True)
    payload = {
        "model": EMBEDDING_MODEL,
        "dim": EMBEDDING_DIM,
        "episode_id": episode_id,
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cue_count": len(cues_with_vectors),
        "cues": cues_with_vectors,
    }
    path = os.path.join(output_dir, f"{episode_id}.json")
    with open(path, "w") as f:
        json.dump(payload, f)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Fireside embedding index")
    parser.add_argument("--output", default="./embeddings.json",
                        help="Output JSON path for the chapter+episode bundle")
    parser.add_argument("--episode-output-dir", default="./episode-embeddings",
                        help="Directory for per-episode cue-embedding JSON files")
    parser.add_argument("--skip-cues", action="store_true",
                        help="Skip per-episode cue-level embeddings (chapter-level only)")
    parser.add_argument("--upload", action="store_true",
                        help="Upload bundle (and per-episode files unless --skip-cues) to S3")
    args = parser.parse_args()

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    client = OpenAI(api_key=api_key)

    print("Fetching Sanity catalog…")
    episodes = fetch_sanity_episodes()
    print(f"  {len(episodes)} episodes")

    episode_index: dict = {}
    chapter_index: List[dict] = []
    cue_files_written: List[str] = []
    total_cues_embedded = 0

    for i, ep in enumerate(episodes, 1):
        ep_id = ep.get("_id")
        title = ep.get("title", "(untitled)")
        if not ep_id:
            print(f"  [{i}/{len(episodes)}] skip — no _id: {title}")
            continue

        print(f"  [{i}/{len(episodes)}] {title}")

        transcript_url = ep.get("transcriptURL")
        cues = fetch_vtt_cues(transcript_url) if transcript_url else []
        full_transcript = cues_concatenated(cues)

        # Episode-level embedding — same as before
        ep_text = build_episode_embedding_text(ep, full_transcript)
        try:
            ep_vector = embed(client, ep_text)
        except Exception as e:
            print(f"    episode embed failed: {e}", file=sys.stderr)
            continue
        episode_index[ep_id] = {
            "title": title,
            "series": ep.get("seriesTitle") or "",
            "vector": ep_vector,
        }

        # Chapter-level embeddings. Skip when there are no chapter
        # timecodes (full episodes without chapter markers).
        chapters = ep.get("chapters") or []
        for chapter in chapters:
            ch_num = chapter.get("chapterNumber")
            ch_title = chapter.get("title") or ""
            t_in = parse_timecode(chapter.get("timeCodeIn"))
            t_out = parse_timecode(chapter.get("timeCodeOut"))
            if t_in is None or t_out is None:
                continue
            ch_transcript = cues_within_range(cues, t_in, t_out) if cues else ""
            ch_text = build_chapter_embedding_text(ep, chapter, ch_transcript)
            try:
                ch_vector = embed(client, ch_text)
            except Exception as e:
                print(f"    chapter {ch_num} embed failed: {e}", file=sys.stderr)
                continue
            chapter_index.append({
                "episode_id": ep_id,
                "chapter_number": ch_num if ch_num is not None else 0,
                "title": ch_title,
                "vector": ch_vector,
            })

        # Per-episode cue-level embeddings — powers in-episode search.
        # Skipped when transcript is missing or --skip-cues was passed.
        if not args.skip_cues and cues:
            cues_with_vectors = embed_cues_for_episode(client, cues)
            if cues_with_vectors:
                path = write_episode_cue_file(ep_id, cues_with_vectors, args.episode_output_dir)
                cue_files_written.append(path)
                total_cues_embedded += len(cues_with_vectors)
                print(f"    {len(cues_with_vectors)} cue embeddings → {path}")

    output = {
        "model": EMBEDDING_MODEL,
        "dim": EMBEDDING_DIM,
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "episodes": episode_index,
        "chapters": chapter_index,
    }

    with open(args.output, "w") as f:
        json.dump(output, f)
    bytes_written = os.path.getsize(args.output)
    print(
        f"\n✅ Wrote {len(episode_index)} episodes + {len(chapter_index)} chapters "
        f"({bytes_written / 1024:.1f} KB) → {args.output}"
    )
    if cue_files_written:
        print(
            f"✅ Wrote {len(cue_files_written)} per-episode cue files "
            f"({total_cues_embedded} cues total) → {args.episode_output_dir}/"
        )

    if args.upload:
        upload_to_s3(args.output)
        if cue_files_written:
            upload_episode_embeddings_to_s3(args.episode_output_dir)


if __name__ == "__main__":
    main()
