from .project import analyze
from .inputs import SPLITS, normalize_rows


# 换槽批次复算后用于判定“主条、档位、工位均未变”的三个口径。
BATCH_IDENTITY = ('strip_id', 'priority', 'owner')


def recalculate(rows, settings, excluded_ids):
    """沿用原当班样本与运行参数，剔除已确认记错的历史控制条后复算。

    剔除只允许作用于历史参考分组；当班样本保持不变。规范化会重新执行
    全套台账校验，样本数等条件不满足时抛出 ValueError，调用方维持暂停。
    """
    excluded = set(excluded_ids)
    splits_by_id = {row['id']: row['split'] for row in rows}
    unknown = excluded - splits_by_id.keys()
    if unknown:
        raise ValueError(f"台账中找不到控制条：{', '.join(sorted(unknown))}")
    if any(splits_by_id[strip_id] == 'shift' for strip_id in excluded):
        raise ValueError('仅可剔除历史参考控制条，当班样本须保持不变')
    kept = [row for row in rows if row['id'] not in excluded]
    if len(kept) == len(rows):
        raise ValueError('没有可剔除的控制条记录')
    # normalize_rows 负责重新校验分组条数、时序与测量口径。
    normalized = normalize_rows(kept)
    return analyze(normalized, settings)

def revisit_index(result):
    table = next(table for table in result['tables'] if table['key'] == 'revisit_queue')
    return {row['change_id']: row for row in table['rows']}


def compare_batches(previous_result, new_result):
    """逐换槽批次比对复算前后的主条、档位与工位。"""
    previous = revisit_index(previous_result)
    current = revisit_index(new_result)
    previous_ids, current_ids = set(previous), set(current)

    unchanged, changed = [], []
    for change_id in sorted(previous_ids & current_ids):
        before, after = previous[change_id], current[change_id]
        fields = {
            key: {'before': before[key], 'after': after[key]}
            for key in BATCH_IDENTITY
            if before[key] != after[key]
        }
        target = unchanged if not fields else changed
        target.append({'change_id': change_id, 'changed_fields': fields})

    def removed_rows(ids):
        return [{'change_id': change_id, **{key: previous[change_id][key] for key in BATCH_IDENTITY}}
                for change_id in sorted(ids)]

    def added_rows(ids):
        return [{'change_id': change_id, **{key: current[change_id][key] for key in BATCH_IDENTITY}}
                for change_id in sorted(ids)]

    return {
        'unchanged_batches': unchanged,
        'changed_batches': changed,
        'added_batches': added_rows(current_ids - previous_ids),
        'removed_batches': removed_rows(previous_ids - current_ids),
    }


def withdrawn_split(rows, strip_id):
    for row in rows:
        if row['id'] == strip_id:
            return row['split']
    return None


def split_label(split):
    return SPLITS.get(split, split)
