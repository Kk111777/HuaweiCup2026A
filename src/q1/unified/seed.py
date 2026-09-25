"""Lift a freshly computed official singlecore result to idle extra cores."""
from copy import deepcopy
def extend_empty_cores(plan, result, cores):
    plan,result=deepcopy(plan),deepcopy(result)
    old_cores=len(plan['core_schedules'])
    if cores<old_cores:
        raise ValueError('cannot shrink a verified prior-core solution')
    plan['core_schedules'].extend([] for _ in range(cores-old_cores))
    result['num_cores']=cores
    result['memory_peak_by_core']={str(k):v for k,v in result['memory_peak_by_core'].items()}
    for c in range(old_cores,cores):
        result['memory_peak_by_core'][str(c)]={'L1':0,'UB':0}
        result['per_core_timeline'].append({'core_id':c,'tasks':[],'subgraphs':[],'ops':[]})
    result.pop('execution_mode',None)
    return plan,result
