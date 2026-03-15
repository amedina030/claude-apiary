import math
import re
from collections import Counter

# Common English stop words to ignore during TF-IDF
_STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "it", "be", "as", "was", "are",
    "this", "that", "i", "you", "we", "they", "he", "she", "will", "have",
    "do", "not", "so", "if", "my", "your", "its", "me", "let", "now",
}


def _tokenize(text):
    tokens = re.findall(r"[a-z]+", text.lower())
    return [t for t in tokens if t not in _STOP_WORDS and len(t) > 1]


def _tfidf_vectors(corpus):
    """Return list of TF-IDF dicts, one per document."""
    tokenized = [_tokenize(doc) for doc in corpus]
    n = len(tokenized)

    # Document frequency
    df = Counter()
    for tokens in tokenized:
        df.update(set(tokens))

    vectors = []
    for tokens in tokenized:
        tf = Counter(tokens)
        total = len(tokens) or 1
        vec = {}
        for term, count in tf.items():
            tfidf = (count / total) * math.log((n + 1) / (df[term] + 1))
            vec[term] = tfidf
        vectors.append(vec)

    return vectors


def _cosine(vec_a, vec_b):
    if not vec_a or not vec_b:
        return 0.0
    dot = sum(vec_a.get(t, 0) * v for t, v in vec_b.items())
    norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
    norm_b = math.sqrt(sum(v * v for v in vec_b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _median(values):
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return (s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2)


def _percentile(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    idx = (p / 100) * (len(s) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def _group_by_response(log_entries):
    """
    Group log entries by (session_id, task_turn), summing net_tokens_delta per group.
    Falls back to turn_number for old entries that predate task_turn.
    Returns list of (assistant_message, total_net_tokens_delta) tuples.
    Entries without turn_number are skipped (old format).
    """
    groups = {}
    for e in log_entries:
        if "turn_number" not in e:
            continue
        task_turn = e.get("task_turn", e["turn_number"])
        key = (e.get("session_id", ""), task_turn)
        if key not in groups:
            groups[key] = {"message": e.get("assistant_message", ""), "total": 0}
        groups[key]["total"] += e.get("net_tokens_delta", 0)
    return [(g["message"], g["total"]) for g in groups.values()]


def estimate(assistant_message, log_entries, top_n=10, percentile=90, token_threshold=None):
    """
    Compare assistant_message against historical responses using TF-IDF cosine similarity.
    Log entries are grouped by (session_id, task_turn) so the threshold is compared
    against total task cost, not individual tool call cost. task_turn chains
    continuation turns (user replies to mid-task clarifying questions) back to
    the originating request turn.

    Returns:
        (is_expensive, median_similar_tokens, threshold_used)
    """
    if not log_entries or not assistant_message.strip():
        return False, 0, 0

    responses = _group_by_response(log_entries)
    if not responses:
        return False, 0, 0

    historical_messages = [r[0] for r in responses]
    response_costs = [r[1] for r in responses]

    corpus = historical_messages + [assistant_message]
    vectors = _tfidf_vectors(corpus)

    current_vec = vectors[-1]
    similarities = [_cosine(current_vec, v) for v in vectors[:-1]]

    top_n = min(top_n, len(similarities))
    top_indices = sorted(range(len(similarities)), key=lambda i: similarities[i])[-top_n:]

    similar_costs = [response_costs[i] for i in top_indices]
    median_similar = _median(similar_costs)

    threshold = float(token_threshold) if token_threshold is not None else _percentile(response_costs, percentile)

    return median_similar > threshold, median_similar, threshold
