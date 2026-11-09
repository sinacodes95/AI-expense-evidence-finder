"""Small-corpus hybrid retrieval and deterministic citation validation."""

import json
import math
import re
from collections import Counter

from app.database import Database
from app.provider import AnswerDraft

STOP_WORDS = set("a an and are as at be by can did do does for from had has have how i in is it its me of on or our that the their there these this to was we were what when which who with would you your".split())


def tokens(text: str) -> list[str]:
    return [word for word in re.findall(r"\w+", text.casefold()) if word not in STOP_WORDS]


def keyword_ranking(question: str, chunks: list[dict]) -> list[int]:
    """BM25 over the selected client's chunks, with filenames included for IDs."""
    query = set(tokens(question))
    counts = [Counter(tokens(row["filename"] + " " + row["text"])) for row in chunks]
    lengths = [sum(count.values()) for count in counts]
    average = sum(lengths) / max(len(lengths), 1) or 1
    frequency = Counter(term for count in counts for term in count)
    scores = []
    for index, count in enumerate(counts):
        score = 0.0
        for term in query:
            tf = count[term]
            if not tf:
                continue
            idf = math.log(1 + (len(chunks) - frequency[term] + 0.5) / (frequency[term] + 0.5))
            score += idf * tf * 2.5 / (tf + 1.5 * (0.25 + 0.75 * lengths[index] / average))
        if score > 0:
            scores.append((index, score))
    return [index for index, _ in sorted(scores, key=lambda pair: pair[1], reverse=True)]


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("Embedding dimensions changed. Re-index the documents.")
    denominator = math.sqrt(sum(x * x for x in left) * sum(y * y for y in right))
    return sum(x * y for x, y in zip(left, right)) / denominator if denominator else 0.0


def retrieve(database: Database, client_id: str, question: str, provider=None,
             embedding_model: str = "", limit: int = 6) -> list[dict]:
    chunks = database.chunks(client_id)
    if not chunks:
        return []
    lexical = keyword_ranking(question, chunks)
    if provider is None:
        return [chunks[index] for index in lexical[:limit]]

    missing = [row for row in chunks if row["embedding_model"] != embedding_model or not row["embedding"]]
    if missing:
        vectors = provider.embed([row["text"] for row in missing])
        if len(vectors) != len(missing):
            raise ValueError("The embedding provider returned an incomplete result.")
        with database.connect() as db:
            for row, vector in zip(missing, vectors):
                if not vector or not all(math.isfinite(value) for value in vector):
                    raise ValueError("The embedding provider returned an invalid vector.")
                row["embedding"] = json.dumps(vector)
                db.execute("UPDATE chunks SET embedding=?, embedding_model=? WHERE id=?",
                           (row["embedding"], embedding_model, row["id"]))

    query = provider.embed([question])[0]
    semantic = sorted(range(len(chunks)), key=lambda index: cosine(query, json.loads(chunks[index]["embedding"])), reverse=True)
    # Reciprocal rank fusion avoids treating keyword and cosine scores as the same scale.
    scores = Counter()
    for ranking in (lexical[:30], semantic[:30]):
        for rank, index in enumerate(ranking, start=1):
            scores[index] += 1 / (60 + rank)
    return [chunks[index] for index, _ in scores.most_common(limit)]


def normalize(text: str) -> str:
    return " ".join(text.split())


def source(row: dict, number: int, quote: str = "") -> dict:
    return {"number": number, "document_id": row["document_id"], "filename": row["filename"],
            "page": row["page"], "quote": quote, "context": row["text"]}


def insufficient(message: str = "The retrieved documents do not provide enough evidence to answer this question.") -> dict:
    return {"status": "insufficient_evidence", "message": message, "claims": [], "sources": []}


def validate_answer(draft: AnswerDraft, chunks: list[dict]) -> dict:
    """Reject the entire answer if any citation is missing, invented, or misquoted.

    This verifies provenance, not whether a quoted passage logically supports a claim.
    The accountant must still review the evidence.
    """
    if draft.status == "insufficient_evidence" or not draft.claims:
        return insufficient()
    lookup = {row["id"]: row for row in chunks}
    sources, claims, known = [], [], {}
    for claim in draft.claims:
        if not claim.text.strip() or not claim.evidence:
            return insufficient("The generated answer could not be verified against its sources. Try a more specific question.")
        numbers = []
        for reference in claim.evidence:
            row = lookup.get(reference.chunk_id)
            quote = normalize(reference.quote)
            if row is None or len(quote) < 8 or quote not in normalize(row["text"]):
                return insufficient("The generated answer contained an unverifiable citation. No answer was shown.")
            key = (reference.chunk_id, quote)
            if key not in known:
                known[key] = len(sources) + 1
                sources.append(source(row, known[key], quote))
            if known[key] not in numbers:
                numbers.append(known[key])
        claims.append({"text": claim.text.strip(), "citations": numbers})
    if draft.status == "conflicting_evidence" and len(sources) < 2:
        return insufficient("The reported conflict could not be verified against two supporting passages.")
    return {"status": draft.status, "message": "The sources disagree. Review both before drawing a conclusion." if draft.status == "conflicting_evidence" else "",
            "claims": claims, "sources": sources}


def ask(database: Database, client_id: str, question: str, provider=None, embedding_model: str = "") -> dict:
    chunks = retrieve(database, client_id, question, provider, embedding_model)
    if not chunks:
        return insufficient("No matching passages were found in this client's documents. Try a supplier name, invoice number, or different wording.")
    if provider is None:
        return {"status": "search_only", "message": "Matching passages are shown below. Source search does not generate or verify an answer.",
                "claims": [], "sources": [source(row, number) for number, row in enumerate(chunks, start=1)]}
    return validate_answer(provider.answer(question, chunks), chunks)
