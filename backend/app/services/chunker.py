import re
import tiktoken


def chunk_text(
    text: str,
    chunk_size: int = 500,
    overlap: int = 50,
) -> list[dict]:
    """Split text into overlapping chunks at sentence boundaries.

    Returns list of {"index": int, "text": str, "token_count": int}.
    """
    enc = tiktoken.get_encoding("cl100k_base")

    # Split on sentence boundaries: ., !, ? followed by whitespace or end
    sentence_pattern = re.compile(r"(?<=[.!?])\s+")
    sentences = sentence_pattern.split(text.strip())
    # Re-attach punctuation that got split off and filter empties
    sentences = [s.strip() for s in sentences if s.strip()]

    if not sentences:
        return []

    chunks: list[dict] = []
    current_tokens: list[int] = []
    current_text_parts: list[str] = []

    def flush_chunk(index: int) -> tuple[list[int], list[str]]:
        """Emit a chunk and return overlap tokens/text."""
        if not current_tokens:
            return [], []
        chunk_text_str = " ".join(current_text_parts)
        chunks.append({
            "index": index,
            "text": chunk_text_str,
            "token_count": len(current_tokens),
        })
        # Keep overlap: take the last `overlap` tokens as seed for next chunk
        if overlap > 0 and len(current_tokens) > overlap:
            overlap_tokens = current_tokens[-overlap:]
            # Find how many sentences from the end cover those tokens
            overlap_parts: list[str] = []
            running = 0
            for part in reversed(current_text_parts):
                part_tokens = enc.encode(part)
                running += len(part_tokens)
                overlap_parts.insert(0, part)
                if running >= overlap:
                    break
            return list(overlap_tokens), overlap_parts
        return [], []

    chunk_index = 0

    for sentence in sentences:
        sentence_tokens = enc.encode(sentence)

        # If a single sentence exceeds chunk_size, split it by token windows
        if len(sentence_tokens) > chunk_size:
            # Flush whatever is pending first
            if current_tokens:
                leftover_tokens, leftover_parts = flush_chunk(chunk_index)
                chunk_index += 1
                current_tokens = leftover_tokens
                current_text_parts = leftover_parts

            # Emit this oversized sentence in token-window slices
            for start in range(0, len(sentence_tokens), chunk_size - overlap):
                slice_tokens = sentence_tokens[start: start + chunk_size]
                slice_text = enc.decode(slice_tokens)
                chunks.append({
                    "index": chunk_index,
                    "text": slice_text,
                    "token_count": len(slice_tokens),
                })
                chunk_index += 1
            # Seed next chunk with overlap from end of oversized sentence
            if overlap > 0:
                current_tokens = sentence_tokens[-overlap:]
                current_text_parts = [enc.decode(current_tokens)]
            continue

        # Would this sentence overflow the current chunk?
        if current_tokens and len(current_tokens) + len(sentence_tokens) > chunk_size:
            leftover_tokens, leftover_parts = flush_chunk(chunk_index)
            chunk_index += 1
            current_tokens = leftover_tokens
            current_text_parts = leftover_parts

        current_tokens.extend(sentence_tokens)
        current_text_parts.append(sentence)

    # Flush remaining
    if current_tokens:
        flush_chunk(chunk_index)

    return chunks
