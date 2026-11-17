"""Human review marks are valid only for the exact text and timing reviewed."""
import hashlib
import json
import math

FIELDS = {'review_status', 'review_signature'}


def signature(row):
    basis = [row['text'].strip(), row.get('zh', '').strip(), row['speaker'],
             math.floor(float(row['start']) * 1000 + .5), math.floor(float(row['end']) * 1000 + .5)]
    return hashlib.sha256(json.dumps(basis, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def review_fields(row):
    status = row.get('review_status', 'unreviewed')
    if not isinstance(status, str) or status not in {'unreviewed', 'checked', 'question'}:
        raise ValueError('无效的人工核对标记')
    if status != 'unreviewed' and row.get('review_signature') == signature(row):
        return dict(review_status=status, review_signature=row['review_signature'])
    return {}


def normalized(rows):
    return [{**{k: v for k, v in row.items() if k not in FIELDS}, **review_fields(row)} for row in rows]


def content(rows):
    return [{k: v for k, v in row.items() if k not in FIELDS} for row in rows]
