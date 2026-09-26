"""只读官方前缀对照实现，不是完整Q2求解器。

由当前附件 _build_scene_b_tasks 中“任务构图”部分静态提取。
停在 Step1 后，不调用 Step2、Step3 或全局模拟。
必须先把 --reference-root/code 加入 sys.path；由探针执行SHA兼容性检查。
复制此文件进入工程时保留来源；不要改动官方文件。
"""
import math
from multicore_cut_evaluate_problem_2 import _append_op, _append_tensor, _prioritize_task_seq
from multicore_cut_evaluate_problem_1 import _copy_traffic_bytes, _original_tensor_views
from schedule_step1 import step1_schedule
from stub_multicore_cut_and_schedule import derive_multicore_plan

def build_scene_b_prefix(graph_json, plan, bandwidth, capacity):
    """每核合并为一个 Task，仅对跨核边插入 DDR COPY 对。

    原图/方案由本函数开头的derive_multicore_plan进行入口校验，参数由
    evaluate_scene_b校验。局部图保留本核依赖并插入源/汇COPY，仍为DAG；
    子图重排的拓扑性由_prioritize_task_seq保证，Step2/3无需重复检查。
    """
    plan_view = derive_multicore_plan(graph_json, plan)
    num_cores = plan_view['num_cores']
    op_by_id = {op['id']: op for op in graph_json['ops']}
    tensor_by_id = {tensor['id']: tensor for tensor in graph_json['tensors']}
    mapping = plan_view['mapping']
    core_by_subgraph = plan_view['core_by_subgraph']
    core_by_op = {op_id: core_by_subgraph[subgraph_id] for op_id, subgraph_id in mapping.items()}
    core_orders = plan_view['core_orders']
    # 与 list.index 完全同序，避免每条 COPY 重扫核心子图列表。
    order_rank = {k: {j: i for i, j in enumerate(q)} for k, q in core_orders.items()}
    producers, consumers, direct_edges = _original_tensor_views(graph_json)
    tasks_data = {core_id: {'ops': [], 'tensors': {}, 'edges': [], 'op_subgraph': {}, 'subgraph_order': core_orders.get(core_id, [])} for core_id in range(num_cores)}
    for op_id, subgraph_id in mapping.items():
        _append_op(tasks_data[core_by_op[op_id]], op_by_id[op_id], subgraph_id)
    max_node_id = max([op['id'] for op in graph_json['ops']] + [tensor['id'] for tensor in graph_json['tensors']] + [0])
    next_id = max_node_id + 1
    cross_links = []
    cross_task_traffic = 0

    def new_id():
        nonlocal next_id
        value = next_id
        next_id += 1
        return value

    def add_copy_in(core_id, local_tid, size, subgraph_id, ddr_tid=None):
        ddr_tid = new_id() if ddr_tid is None else ddr_tid
        copy_id = new_id()
        _append_tensor(tasks_data[core_id], {'id': ddr_tid, 'pos': 'DDR', 'size': size})
        _append_op(tasks_data[core_id], {'id': copy_id, 'op': 'COPY_IN', 'pipe': 'PIPE_MTE2', 'cycles': max(1, math.ceil(size / bandwidth))}, subgraph_id)
        tasks_data[core_id]['edges'].extend([{'source': ddr_tid, 'target': copy_id}, {'source': copy_id, 'target': local_tid}])
        return (copy_id, ddr_tid)

    def add_copy_out(core_id, local_tid, size, subgraph_id, ddr_tid=None):
        ddr_tid = new_id() if ddr_tid is None else ddr_tid
        copy_id = new_id()
        _append_tensor(tasks_data[core_id], {'id': ddr_tid, 'pos': 'DDR', 'size': size})
        _append_op(tasks_data[core_id], {'id': copy_id, 'op': 'COPY_OUT', 'pipe': 'PIPE_MTE3', 'cycles': max(1, math.ceil(size / bandwidth))}, subgraph_id)
        tasks_data[core_id]['edges'].extend([{'source': local_tid, 'target': copy_id}, {'source': copy_id, 'target': ddr_tid}])
        return (copy_id, ddr_tid)
    for tensor_id in sorted(tensor_by_id):
        tensor = tensor_by_id[tensor_id]
        eligible_producers = sorted((op for op in producers.get(tensor_id, ()) if op in mapping))
        eligible_consumers = sorted((op for op in consumers.get(tensor_id, ()) if op in mapping))
        producer_cores = sorted({core_by_op[op] for op in eligible_producers})
        consumer_cores = sorted({core_by_op[op] for op in eligible_consumers})
        touched_cores = sorted(set(producer_cores) | set(consumer_cores))
        if not touched_cores:
            continue
        local_tensor = dict(tensor)
        if local_tensor.get('pos') == 'DDR':
            local_tensor['pos'] = 'UB'
        for core_id in touched_cores:
            _append_tensor(tasks_data[core_id], local_tensor)
            for op_id in eligible_producers:
                if core_by_op[op_id] == core_id:
                    tasks_data[core_id]['edges'].append({'source': op_id, 'target': tensor_id})
            for op_id in eligible_consumers:
                if core_by_op[op_id] == core_id:
                    tasks_data[core_id]['edges'].append({'source': tensor_id, 'target': op_id})
        if eligible_consumers and (not eligible_producers):
            for dst_core in consumer_cores:
                dst_ops = [op for op in eligible_consumers if core_by_op[op] == dst_core]
                dst_subgraph = min((mapping[op] for op in dst_ops), key=order_rank[dst_core].__getitem__)
                add_copy_in(dst_core, tensor_id, tensor['size'], dst_subgraph)
        has_original_copy_out = any((op_by_id[op_id].get('op') == 'COPY_OUT' for op_id in consumers.get(tensor_id, ()) if op_id in op_by_id))
        if eligible_producers and (has_original_copy_out or not eligible_consumers):
            for src_core in producer_cores:
                src_ops = [op for op in eligible_producers if core_by_op[op] == src_core]
                src_subgraph = max((mapping[op] for op in src_ops), key=order_rank[src_core].__getitem__)
                add_copy_out(src_core, tensor_id, tensor['size'], src_subgraph)
        for src_core in producer_cores:
            src_ops = [op for op in eligible_producers if core_by_op[op] == src_core]
            src_subgraph = max((mapping[op] for op in src_ops), key=order_rank[src_core].__getitem__)
            for dst_core in consumer_cores:
                if src_core == dst_core:
                    continue
                dst_ops = [op for op in eligible_consumers if core_by_op[op] == dst_core]
                dst_subgraph = min((mapping[op] for op in dst_ops), key=order_rank[dst_core].__getitem__)
                ddr_tid = new_id()
                out_id, _ = add_copy_out(src_core, tensor_id, tensor['size'], src_subgraph, ddr_tid)
                in_id, _ = add_copy_in(dst_core, tensor_id, tensor['size'], dst_subgraph, ddr_tid)
                cross_links.append({'tensor_id': tensor_id, 'size': tensor['size'], 'source_core': src_core, 'target_core': dst_core, 'source_copy_out_id': out_id, 'target_copy_in_id': in_id})
                cross_task_traffic += tensor['size']
    for edge in direct_edges:
        src, dst = (edge['source'], edge['target'])
        if src not in mapping or dst not in mapping:
            continue
        src_core, dst_core = (core_by_op[src], core_by_op[dst])
        if src_core == dst_core:
            tasks_data[src_core]['edges'].append(dict(edge))
            continue
        size = max(0, int(edge.get('data_size', 0)))
        local_tid, ddr_tid = (new_id(), new_id())
        local_tensor = {'id': local_tid, 'pos': 'UB', 'size': size}
        _append_tensor(tasks_data[src_core], local_tensor)
        _append_tensor(tasks_data[dst_core], local_tensor)
        tasks_data[src_core]['edges'].append({'source': src, 'target': local_tid})
        tasks_data[dst_core]['edges'].append({'source': local_tid, 'target': dst})
        out_id, _ = add_copy_out(src_core, local_tid, size, mapping[src], ddr_tid)
        in_id, _ = add_copy_in(dst_core, local_tid, size, mapping[dst], ddr_tid)
        cross_links.append({'tensor_id': local_tid, 'size': size, 'source_core': src_core, 'target_core': dst_core, 'source_copy_out_id': out_id, 'target_copy_in_id': in_id})
        cross_task_traffic += size
    cores = {}
    for core_id in range(num_cores):
        data = tasks_data[core_id]
        graph = {'ops': data['ops'], 'tensors': list(data['tensors'].values()), 'edges': data['edges']}
        raw_seq = step1_schedule(graph) if graph['ops'] else []
        seq = _prioritize_task_seq(graph, raw_seq, data['op_subgraph'], data['subgraph_order'])
        cores[core_id] = {'graph': graph, 'raw_seq': raw_seq, 'seq': seq, 'op_subgraph': dict(data['op_subgraph']), 'subgraph_order': list(data['subgraph_order'])}
    return {'cores': cores, 'cross_links': cross_links, 'cross_task_traffic': cross_task_traffic, 'base_copy_bytes': sum((_copy_traffic_bytes(c['graph']) for c in cores.values())), 'original_copy_bytes': _copy_traffic_bytes(graph_json), 'plan_view': plan_view}
