"""Versioned vendor observations for inspection and field-aware research.

This is deliberately distinct from the legacy execution clock: source labels
remain unverified, session endpoints remain present, and OI state is not a trade.
No database, network, default strategy switch, or price/time imputation.
"""
from copy import deepcopy
from datetime import datetime
import gzip
import hashlib
import json
import math
from pathlib import Path


def _instant(value):
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('label bounds must have a UTC offset')
    return stamp


class ObservationBundle:
    def __init__(self, root):
        self.root = Path(root)
        self.manifest = json.loads((self.root / 'manifest.json').read_text())
        if self.manifest.get('format') != 'minute-observations-v1':
            raise ValueError('unsupported observation bundle format')
        self.entries = {}
        for entry in self.manifest['entries']:
            key = (entry['code'], entry['day'])
            if key in self.entries:
                raise ValueError('duplicate observation partition')
            name = entry['file']
            if Path(name).name != name or name in ('.', '..'):
                raise ValueError('invalid partition file')
            self.entries[key] = dict(entry)

    def read(self, code, day, *, lower=None, upper=None, require_fields=()):
        lower = _instant(lower) if lower is not None else None
        upper = _instant(upper) if upper is not None else None
        if lower is not None and upper is not None and lower > upper:
            raise ValueError('reversed source-label range')
        key = (code, str(day))
        if key not in self.entries:
            raise ValueError('missing observation partition: ' + repr(key))
        entry = self.entries[key]
        path = self.root / entry['file']
        if path.is_symlink():
            raise ValueError('partition symlink refused')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError('observation partition hash mismatch')
        payload = json.loads(gzip.decompress(raw))
        if (payload['code'], payload['day']) != key or payload.get('policy_version') != 'exchange-aligned-candidate-v1':
            raise ValueError('partition identity/policy mismatch')
        for kind in ('trades', 'states'):
            if len(payload[kind]) != entry[kind]:
                raise ValueError('partition count mismatch')
            stamps = [_instant(r['source_label']) for r in payload[kind]]
            if any(b <= a for a, b in zip(stamps, stamps[1:])):
                raise ValueError('duplicate or unordered source labels')
            payload[kind] = [r for r, stamp in zip(payload[kind], stamps)
                             if (lower is None or stamp >= lower) and (upper is None or stamp <= upper)]
        allowed = {'open', 'high', 'low', 'close', 'volume', 'amount', 'open_interest'}
        if set(require_fields) - allowed:
            raise ValueError('unknown required field')
        if require_fields and payload['quarantine']:
            raise ValueError('partition contains quarantine; cannot consume incomplete trades')
        for row in payload['trades']:
            for field in require_fields:
                value = row.get(field)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError('required field unavailable: ' + field + ' at ' + row['source_label'])
        payload['range_semantics'] = 'CLOSED_SOURCE_LABEL_RANGE_NOT_EXECUTION_CLOCK'
        payload['quality_status'] = 'UNVERIFIED_SOURCE_SEMANTICS'
        return deepcopy(payload)

    def last_observed_oi(self, code, day, *, at=None):
        data = self.read(code, day, upper=at)
        if data['quarantine']:
            raise ValueError('quarantine prevents reliable final observation')
        points = [(r['source_label'], r['open_interest'], kind)
                  for kind in ('trades', 'states') for r in data[kind]
                  if r.get('open_interest') is not None]
        if not points:
            return None
        label, value, kind = max(points, key=lambda p: _instant(p[0]))
        return {'source_label': label, 'value': value, 'kind': kind,
                'basis': 'LAST_OBSERVED_STATE_NOT_CERTIFIED_SETTLEMENT'}

    def execution_bars(self, code, day):
        self.read(code, day)
        raise ValueError('vendor label semantics unverified; no legacy execution-clock conversion')
