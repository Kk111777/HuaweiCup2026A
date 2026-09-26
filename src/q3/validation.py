"""Independent checks of actual global operation times and physical lifetimes."""
from collections import defaultdict


def audit_result(bundle, result, settings):
    tasks, links, _, traffic, _ = bundle
    times = {c['core_id']: {o['op_id']: o for o in c['ops']} for c in result['per_core_timeline']}
    peaks, tensor_count, dependencies = {}, 0, 0
    ddr_bytes = cache_bytes = 0
    for core, task in tasks.items():
        timeline = times[core]
        if set(timeline) != set(task['op_by_id']):
            raise AssertionError('operation coverage differs from the prepared execution')
        for op, predecessors in task['op_preds'].items():
            for pred in predecessors:
                dependencies += 1
                if timeline[pred]['end'] > timeline[op]['start']:
                    raise AssertionError('data/memory dependency violated')
        for order in task['pipe_ops'].values():
            for a, b in zip(order, order[1:]):
                if timeline[a]['end'] > timeline[b]['start']:
                    raise AssertionError('same-Pipe overlap or order violation')
        producers, consumers = defaultdict(list), defaultdict(list)
        for op, tids in task['out_tids'].items():
            for tid in tids:
                producers[tid].append(op)
        for op, tids in task['in_tids'].items():
            for tid in tids:
                consumers[tid].append(op)
        events = defaultdict(list)
        for tid, tensor in task['tensor_by_id'].items():
            if tensor['pos'] not in settings.scene.capacity:
                continue
            touched = producers[tid] + consumers[tid]
            if not touched:
                continue
            start = min(timeline[o]['start'] for o in producers[tid] or consumers[tid])
            end = max(timeline[o]['end'] for o in consumers[tid] or producers[tid])
            if end < start:
                raise AssertionError('negative physical lifetime')
            if end > start:
                events[tensor['pos']].extend(((start, 1, tensor['size']), (end, 0, -tensor['size'])))
            tensor_count += 1
        peaks[core] = {}
        for pos, capacity in settings.scene.capacity.items():
            used = peak = 0
            for _, _, delta in sorted(events[pos]):
                used += delta
                peak = max(peak, used)
                if used < 0 or used > capacity:
                    raise AssertionError(f'global physical capacity violated: core={core} {pos} used={used}')
            if used != 0:
                raise AssertionError('physical tensors remain after execution')
            peaks[core][pos] = peak
        for op, entry in timeline.items():
            if entry.get('memory_path') not in ('DDR', 'CACHE_READ'):
                continue
            # Match _op_duration: COPY_IN output bytes, COPY_OUT input bytes.
            tids = task['out_tids'][op] if task['op_by_id'][op]['op'] == 'COPY_IN' else task['in_tids'][op]
            size = sum(task['tensor_by_id'][t]['size'] for t in tids)
            if entry['memory_path'] == 'DDR':
                ddr_bytes += size
            else:
                cache_bytes += size
    for link in links:
        source = times[link['source_core']][link['source_copy_out_id']]
        target = times[link['target_core']][link['target_copy_in_id']]
        if source['end'] + settings.scene.delay > target['start']:
            raise AssertionError('cross-core release violated')
    end = max((o['end'] for core in times.values() for o in core.values()), default=0)
    if end != result['makespan'] or result['data_movement_bytes'] != traffic:
        raise AssertionError('official makespan/traffic inconsistent')
    if any(e.get('used_bytes', 0) > settings.cache_capacity for e in result.get('cache_events', [])):
        raise AssertionError('FIFO capacity exceeded')
    return dict(status='PASSED', operation_count=sum(map(len, times.values())),
                physical_tensor_count=tensor_count, dependency_count=dependencies,
                physical_memory_peak_by_core=peaks, actual_ddr_bytes=ddr_bytes, actual_l2_bytes=cache_bytes)
