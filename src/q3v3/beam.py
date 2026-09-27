"""Bounded state diversity includes placement AND observed cache event order."""
import hashlib
import json

from .objective import key


def cache_signature(result):
    digest = hashlib.sha256()
    for event in result.get('cache_events', []):
        value = (event['event'], event.get('core_id'), event.get('tensor_id'),
                 event.get('evicted_tensor_ids', []))
        digest.update(json.dumps(value, separators=(',', ':')).encode())
        digest.update(b'\n')
    return digest.hexdigest()


def retain(items, policy):
    ranked = sorted(items, key=lambda item: (key(item['result'], policy), item['digest']))
    chosen = ranked[:2]
    # One slot for a distinct placement; one for distinct observed cache order.
    # This keeps same-placement order variants explorable beyond the best two.
    for dimension in ('placement', 'cache_signature'):
        used = {v[dimension] for v in chosen}
        candidate = next((v for v in ranked[2:] if v[dimension] not in used
                          and all(v['digest'] != x['digest'] for x in chosen)), None)
        if candidate is not None:
            chosen.append(candidate)
    for candidate in ranked[2:]:
        if len(chosen) >= 4:
            break
        if all(candidate['digest'] != v['digest'] for v in chosen):
            chosen.append(candidate)
    return chosen
